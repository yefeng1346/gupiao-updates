globalThis.candidatePageErrors=[];
page.on('pageerror',error=>globalThis.candidatePageErrors.push(String(error)));
await page.goto('http://127.0.0.1:59517/',{waitUntil:'domcontentloaded'});
await expect(page.getByRole('heading',{name:'综合候选股票与AI解读',exact:true})).toBeVisible();
return await page.locator('#candidateSection').ariaSnapshot();
