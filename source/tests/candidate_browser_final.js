// Prior failed assertion retained the old script instance; inspect first, then reload.
const before=await page.locator('#candidateStatus').innerText();
await page.reload({waitUntil:'domcontentloaded'});
const section=page.locator('#candidateSection');
await expect(section.locator('tbody')).toContainText('模拟股票');
await section.getByRole('spinbutton',{name:'候选数量',exact:true}).fill('11');
await section.getByRole('button',{name:'生成候选股票＋AI解读',exact:true}).click();
await expect(section.getByRole('status')).toContainText('候选数量请填写');
await expect(section.locator('tbody')).toContainText('模拟股票');
await section.getByRole('spinbutton',{name:'候选数量',exact:true}).fill('10');
await section.scrollIntoViewIfNeeded();
return await page.screenshot({fullPage:false});
