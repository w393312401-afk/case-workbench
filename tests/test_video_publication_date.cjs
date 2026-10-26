'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const context=vm.createContext({window:{}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/video-publication.js'),'utf8'),context);
const {postedOn}=context.window.VideoPublication;
const idAt=iso=>String(BigInt(Date.parse(iso)/1000)<<32n);
test('a post ID gives the day the sales export shows, at its fixed UTC-8',()=>{
  // Registered posts whose export rows read 2026-10-17 05:47 and 2026-10-18 06:32.
  assert.equal(postedOn('7697632860966030623'),'2026-10-17');
  assert.equal(postedOn('7698015486365273375'),'2026-10-18');
  assert.equal(postedOn(idAt('2026-10-18T07:59:59Z')),'2026-10-17');
  assert.equal(postedOn(idAt('2026-10-18T08:00:00Z')),'2026-10-18');
  assert.equal(postedOn(' https://www.tiktok.com/@fixture/photo/7697632860966030623?lang=en '),'2026-10-17');
  // Read like normalize reads the saved ID: any other link or label keeps only its digits.
  assert.equal(postedOn('https://m.tiktok.com/v/7697632860966030623.html'),'2026-10-17');
  assert.equal(postedOn('ID: 7697632860966030623'),'2026-10-17');
});
test('IDs that cannot be a past post give no date',()=>{
  // Overlong IDs pass the 6–25 digit check but decode beyond any date; they must not throw or yield '+0577…'.
  for (const value of [undefined,'','123','abc','7561234567','0000000000000000000',idAt('2014-12-31T00:00:00Z'),idAt('2099-01-01T00:00:00Z'),
      '1234567890123456789012','76976328609660306231234','9999999999999999999999999'])
    assert.equal(postedOn(value),'',String(value));
});
