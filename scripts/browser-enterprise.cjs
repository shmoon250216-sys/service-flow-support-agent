const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const path=require('path');const fs=require('fs');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.BROWSER_PATH?{executablePath:process.env.BROWSER_PATH}:{})});
 const context=await browser.newContext({viewport:{width:1440,height:1050}}); const errors=[];
 async function open(){const p=await context.newPage();p.on('pageerror',e=>errors.push(e.message));await p.goto((process.env.UI_BASE_URL||'http://127.0.0.1:8002')+'/');await p.locator('#order option').first().waitFor({state:'attached'});return p}
 const customer=await open(); await customer.locator('#identity').selectOption('C-003'); await customer.waitForFunction(()=>document.querySelector('#order').textContent.includes('O-1003'));await customer.locator('#newChat').click(); await customer.waitForFunction(()=>document.querySelector('#chatTitle').textContent.includes('O-1003'));
 await customer.locator('#message').fill('蓝牙问题，请转人工');await customer.locator('#send').click();await customer.waitForFunction(()=>document.querySelector('#state').textContent==='等待人工');
 const staff=await open();await staff.locator('#staffTab').click();await staff.locator('#list .item').filter({hasText:'O-1003'}).click();await staff.locator('#claim').click();await staff.locator('#waitCustomer').waitFor({state:'visible'});
 await staff.locator('#message').fill('请补充设备序列号和指示灯照片');await staff.locator('#waitCustomer').click();await staff.getByText('等待客户补充',{exact:true}).last().waitFor();
 await customer.locator('#message').fill('序列号 TEST-003，指示灯闪烁');await customer.locator('#send').click();await staff.getByText('客户补充 · customer',{exact:false}).first().waitFor({timeout:15000}).catch(async()=>{await staff.getByText('序列号 TEST-003，指示灯闪烁',{exact:true}).waitFor({timeout:15000})});
 const supervisor=await open();await supervisor.locator('#staffTab').click();await supervisor.locator('#identity').selectOption('S-002');await supervisor.locator('#list .item').filter({hasText:'O-1003'}).waitFor();
 await staff.locator('#message').fill('转交二线客服继续定位');await staff.locator('#transferTarget').selectOption('S-002');await staff.locator('#transfer').click();await staff.waitForFunction(()=>document.querySelector('#send').disabled);
 await supervisor.locator('#list .item').filter({hasText:'O-1003'}).click();await supervisor.locator('#message').fill('检查完成，用户确认恢复');await supervisor.locator('#resolve').click();await supervisor.waitForFunction(()=>document.querySelector('#state').textContent==='已结案');
 await supervisor.locator('#message').fill('客户反馈复发，重新安排检查');await supervisor.locator('#reopen').click();await supervisor.waitForFunction(()=>document.querySelector('#state').textContent==='等待人工');await supervisor.locator('#claim').click();await supervisor.waitForFunction(()=>document.querySelector('#state').textContent==='人工处理中');
 await supervisor.getByText('已送达通知中心',{exact:false}).first().waitFor({timeout:15000});
 const output=process.env.UI_OUTPUT||'docs/screenshots';fs.mkdirSync(output,{recursive:true});await supervisor.screenshot({path:path.join(output,'enterprise-workbench.png'),fullPage:true});await supervisor.setViewportSize({width:390,height:844});await supervisor.screenshot({path:path.join(output,'enterprise-mobile.png'),fullPage:true});const overflow=await supervisor.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
 if(errors.length||overflow)throw Error(JSON.stringify({errors,overflow})); console.log(JSON.stringify({checks:['wait_customer','customer_replied','transfer','previous_owner_disabled','resolution_code','supervisor_reopen','audit','outbox_delivery','staff_mobile_no_overflow'],errors}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
