(function(){
"use strict";
function loadHls(){if(window.Hls)return Promise.resolve(window.Hls);if(window.__hlsPromise)return window.__hlsPromise;window.__hlsPromise=new Promise(function(resolve,reject){var script=document.createElement("script");script.src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js";script.async=true;script.onload=function(){resolve(window.Hls)};script.onerror=function(){reject(new Error("HLS playback library could not be loaded."))};document.head.appendChild(script)});return window.__hlsPromise}
function mount(root){var video=root.querySelector("video"),button=root.querySelector("[data-load-player]"),status=root.querySelector("[data-player-status]"),overlay=root.querySelector(".player-overlay");if(!video||!status)return;var started=false,hls=null,directFile=false;
function message(text){status.textContent=text}
function overlayOn(){if(overlay)overlay.classList.remove("is-hidden")}
function overlayOff(){if(overlay)overlay.classList.add("is-hidden")}
function retryVisible(visible){if(!button)return;button.classList.toggle("is-hidden",!visible);button.disabled=false;button.textContent="Try again"}
async function initialize(){if(started)return;started=true;directFile=false;retryVisible(false);overlayOn();message("Finding the stream…");
try{
 var response=await fetch(root.getAttribute("data-stream-endpoint"),{headers:{"Accept":"application/json"},credentials:"same-origin"});
 var result=await response.json().catch(function(){return{}});
 if(!response.ok||!result.url)throw new Error(result.error||"This video is not currently playable.");
 if(result.type==="file"){
   directFile=true;video.src=result.url;video.preload="metadata";video.load();
   message("Direct video source found. Press play to watch.");overlayOff();return;
 }
 if(video.canPlayType("application/vnd.apple.mpegurl")){
   video.src=result.url;video.preload="metadata";video.load();message("Stream found. Press play to watch.");overlayOff();
 }else{
   var Hls=await loadHls();
   if(!Hls||!Hls.isSupported())throw new Error("This browser does not support HLS playback.");
   hls=new Hls({enableWorker:true,lowLatencyMode:false,backBufferLength:30,maxBufferLength:30});
   hls.on(Hls.Events.MANIFEST_PARSED,function(){message("Stream ready. Press play to watch.");overlayOff()});
   hls.on(Hls.Events.ERROR,function(_event,data){if(data&&data.fatal){if(hls)hls.destroy();hls=null;started=false;message("The stream could not be loaded. It may be unavailable or the media host may block playback.");overlayOn();retryVisible(true)}});
   hls.loadSource(result.url);hls.attachMedia(video);message("Loading video manifest…");
 }
}catch(error){started=false;message(error&&error.message?error.message:"Playback could not be initialized.");overlayOn();retryVisible(true)}
}
video.addEventListener("error",function(){
 if(!directFile)return;
 directFile=false;started=false;
 message("The direct video source could not be played. It may be unavailable or require access on the source site.");
 overlayOn();retryVisible(true);
});
if(button)button.addEventListener("click",initialize);
initialize();
}
document.querySelectorAll("[data-video-player]").forEach(mount);
})();
