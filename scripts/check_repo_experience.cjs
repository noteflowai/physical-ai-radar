// Run with a maintained repository's pinned Playwright installation.
"use strict";
const fs=require("node:fs/promises"),path=require("node:path"),assert=require("node:assert/strict");
const [root,url,output]=process.argv.slice(2);
const {chromium}=require(require.resolve("playwright",{paths:[path.resolve(root)]}));
(async()=>{
  await fs.mkdir(output,{recursive:true});
  const browser=await chromium.launch({headless:true}),results=[];
  try{
    for(const width of [1440,390,320]){
      const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:"reduce",
        extraHTTPHeaders:{"Cache-Control":"no-cache","Pragma":"no-cache"}});
      const errors=[],bad=[];
      page.on("pageerror",e=>errors.push(e.message));
      page.on("response",r=>{if(r.status()>=400)bad.push({url:r.url(),status:r.status()})});
      await page.goto(url,{waitUntil:"networkidle",timeout:60000});
      assert.equal(await page.locator("h1").count(),1,"One clear page heading");
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"Horizontal overflow");
      const body=await page.locator("body").innerText();assert(!body.includes("\ufffd"),"Corrupted public text");
      const controls=page.locator("#resources-image,#proof-image,#preview-image");
      const mustExport=url.includes("dsh-skills-anywhere")||url.includes("/evalarc/")||
        url.includes("github.io/robot-reel");
      if(mustExport)assert.equal(await controls.count(),1,"The published result-export control is missing");
      if(await controls.count()){
        assert(await controls.isEnabled(),"The first result export must be ready");
        const pending=page.waitForEvent("download");
        await controls.click();const download=await pending;
        const bytes=await fs.readFile(await download.path());
        assert.equal(bytes.readUInt32BE(16),1200);assert(bytes.length>15000);
        if(width===1440)await download.saveAs(path.join(output,"result.png"));
      }
      if(await page.locator('[data-model-seed="29"]').count()){
        await page.locator('[data-model-seed="29"]').click();
        assert.match(await page.locator("#proof-open").getAttribute("href"),/seed=29/);
      }
      const video=page.locator(".hero video");
      if(await video.count()){
        assert(await video.evaluate(v=>v.paused),"Recorded motion starts only on demand");
        if(width===1440){
          await video.evaluate(async v=>{await v.play();await new Promise((resolve,reject)=>{
            const timer=setTimeout(()=>reject(new Error("Preview did not advance")),15000);
            v.addEventListener("timeupdate",()=>{clearTimeout(timer);v.pause();resolve()},{once:true});
          })});
        }
      }
      await page.evaluate(()=>window.scrollTo(0,0));
      await page.screenshot({path:path.join(output,`viewport-${width}.png`)});
      assert.deepEqual(errors,[]);assert.deepEqual(bad,[]);
      results.push({width,title:await page.title(),overflow:false,errors,badResponses:bad});
      await page.close();
    }
  }finally{await browser.close()}
  await fs.writeFile(path.join(output,"browser.json"),JSON.stringify({url,results},null,2)+"\n");
  console.log(JSON.stringify({url,viewports:results.length,status:"passed"}));
})().catch(e=>{console.error(e);process.exitCode=1});
