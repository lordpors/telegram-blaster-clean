const QUEUE_SHEET = 'Blast Otomatis';
const DONE_SHEET = 'Selesai';
const HEADERS = ['Username', 'Pesan', 'Interval (detik)', 'Kuota per job', 'ID', 'Jeda antar job (menit)', 'Diklaim pada', 'Status terakhir'];

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Blast')
    .addItem('Acak username', 'acakUsername')
    .addToUi();
}

function acakUsername() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(QUEUE_SHEET);
  const count = sheet.getLastRow() - 2;
  if (count < 2) return;
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const claimed = sheet.getRange(3, 5, count, 1).getValues().some(([id]) => id);
    if (claimed) throw new Error('Masih ada username yang sedang diproses; tunggu job selesai terlebih dahulu.');
    sheet.getRange(3, 1, count, HEADERS.length).randomize();
    SpreadsheetApp.getActive().toast(`${count} baris berhasil diacak`, 'Blast');
  } finally {
    lock.releaseLock();
  }
}

function siapkan() {
  const file = SpreadsheetApp.getActive();
  const queue = file.getSheetByName(QUEUE_SHEET);
  if (!queue) throw new Error(`Tab ${QUEUE_SHEET} tidak ditemukan`);
  if (queue.getRange('A1').getValue() !== HEADERS[0]) {
    queue.insertRowsBefore(1, 2);
    queue.getRange('A2').setValue('Pengaturan');
  }
  if (queue.getRange('D1').getValue() === 'ID') queue.insertColumnAfter(3);
  if (queue.getRange('F1').getValue() === 'Diklaim pada') queue.insertColumnAfter(5);
  queue.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
  if (!queue.getRange('D2').getValue()) queue.getRange('D2').setValue(500);
  if (!queue.getRange('F2').getValue()) queue.getRange('F2').setValue(0);
  queue.hideColumns(5, 1);
  queue.hideColumns(7, 1);
  queue.setFrozenRows(1);
  const done = file.getSheetByName(DONE_SHEET) || file.insertSheet(DONE_SHEET);
  if (!done.getLastRow()) done.appendRow([...HEADERS.slice(0, 3), 'Terkirim pada']);
}

function doPost(event) {
  const body = JSON.parse(event.postData.contents || '{}');
  authorize_(body.secret);
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    ensureSchema_();
    if (body.action === 'claim') return claim_();
    if (body.action === 'settings') return settings_();
    return finish_(body.results || []);
  } finally {
    lock.releaseLock();
  }
}

function ensureSchema_() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(QUEUE_SHEET);
  let changed = false;
  if (sheet.getRange('D1').getValue() === 'ID') {
    sheet.insertColumnAfter(3);
    changed = true;
  }
  if (sheet.getRange('F1').getValue() === 'Diklaim pada') {
    sheet.insertColumnAfter(5);
    changed = true;
  }
  if (!changed) return;
  sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
  if (!sheet.getRange('D2').getValue()) sheet.getRange('D2').setValue(500);
  if (!sheet.getRange('F2').getValue()) sheet.getRange('F2').setValue(0);
  sheet.hideColumns(5, 1);
  sheet.hideColumns(7, 1);
}

function quota_(sheet) {
  const quota = Math.floor(Number(sheet.getRange('D2').getValue()));
  return Number.isFinite(quota) && quota > 0 ? quota : 500;
}

function jobDelayMinutes_(sheet) {
  const delay = Math.floor(Number(sheet.getRange('F2').getValue()));
  return Number.isFinite(delay) && delay > 0 ? delay : 0;
}

function settings_() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(QUEUE_SHEET);
  return json_({
    interval: Number(sheet.getRange('C2').getValue()) || 0,
    quota: quota_(sheet),
    job_delay_minutes: jobDelayMinutes_(sheet),
  });
}

function claim_() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(QUEUE_SHEET);
  const limit = quota_(sheet);
  const message = String(sheet.getRange('B2').getValue()).trim();
  const interval = Number(sheet.getRange('C2').getValue()) || 0;
  if (!message) return json_({items: []});
  const count = sheet.getLastRow() - 2;
  if (count < 1) return json_({items: []});
  const range = sheet.getRange(3, 1, count, HEADERS.length);
  const values = range.getValues();
  const now = new Date();
  const expired = new Date(now.getTime() - 30 * 60 * 1000);
  const items = [];
  values.forEach(row => {
    if (items.length >= limit || !row[0]) return;
    if (row[4] && row[6] && new Date(row[6]) > expired) return;
    const id = row[4] || Utilities.getUuid();
    row[4] = id;
    row[6] = now;
    row[7] = 'Proses';
    items.push({id, username: String(row[0]), message, interval});
  });
  range.setValues(values);
  return json_({items});
}

function finish_(rawResults) {
  const file = SpreadsheetApp.getActive();
  const queue = file.getSheetByName(QUEUE_SHEET);
  const done = file.getSheetByName(DONE_SHEET);
  const count = queue.getLastRow() - 2;
  if (count < 1) return json_({ok: true});
  const range = queue.getRange(3, 1, count, HEADERS.length);
  const values = range.getValues();
  const results = new Map(rawResults.filter(item => item.id).map(item => [String(item.id), item]));
  const completed = [];
  const remove = [];
  values.forEach((row, index) => {
    const result = results.get(String(row[4]));
    if (!result) return;
    if (result.status === 'sent') {
      completed.push([row[0], result.message, result.interval, new Date()]);
      remove.push(index + 3);
    } else {
      row[4] = '';
      row[6] = '';
      row[7] = result.error || result.status;
    }
  });
  range.setValues(values);
  if (completed.length) {
    done.getRange(done.getLastRow() + 1, 1, completed.length, 4).setValues(completed);
  }
  deleteRows_(queue, remove);
  return json_({ok: true});
}

function deleteRows_(sheet, rows) {
  rows.sort((a, b) => b - a);
  let end = rows[0];
  let start = end;
  rows.slice(1).forEach(row => {
    if (row === start - 1) start = row;
    else {
      sheet.deleteRows(start, end - start + 1);
      start = end = row;
    }
  });
  if (start) sheet.deleteRows(start, end - start + 1);
}

function authorize_(secret) {
  const expected = PropertiesService.getScriptProperties().getProperty('SHEET_BLAST_SECRET');
  if (!expected || secret !== expected) throw new Error('Unauthorized');
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
