(() => {
 const root = document.getElementById('guided-flow');
 if (!root) return;
 const id = root.dataset.run, ready = root.dataset.phase !== 'running';
 let state = window.__clusterStory;
 if (state?.cleanup) state.cleanup();
 if (!state || state.id !== id) {
   state = window.__clusterStory = {id, index:0, playing:true, started:false, moved:false};
 }
 const scenes = [...root.querySelectorAll('[data-scene]')];
 const summary = root.querySelector('[data-summary]');
 const controls = root.querySelector('[data-controls]');
 const readout = root.querySelector('[data-readout]');
 const toggle = root.querySelector('[data-toggle]');
 const progressFill = root.querySelector('[data-progress-fill]');
 const progressLabel = root.querySelector('[data-progress-label]');
 const progressPercent = root.querySelector('[data-progress-percent]');
 let timer;
 function stopTimer() {clearTimeout(timer);}
 function schedule() {
   stopTimer();
   if (ready && state.playing && state.index < scenes.length && !document.hidden) {
     timer = setTimeout(() => {
       if (!root.isConnected) return;
       state.index++;
       if (state.index >= scenes.length) state.playing = false;
       paint();
     }, 4000);
   }
 }
 function paint() {
   controls.hidden = !ready;
   const finished = state.index >= scenes.length;
   scenes.forEach((scene,i) => {scene.hidden = i !== state.index;});
   if (summary) summary.hidden = !ready || !finished;
   if (!ready) {
     readout.textContent = '샘플 데이터를 분석하고 있습니다. 완료되면 첫 장면부터 해설합니다.';
   } else if (finished) {
     readout.textContent = root.dataset.phase === 'error' ? '해설 종료 · 실행이 중단되어 전체 결론을 내릴 수 없습니다.' : '해설 완료 · 관측부터 판단까지의 흐름을 확인했습니다.';
   } else {
     readout.textContent = `장면 ${state.index+1}/${scenes.length} · ${state.playing ? '자동 전개 · 약 4초 간격' : '일시정지'} · ${scenes[state.index].querySelector('h4').textContent}`;
   }
   const total = scenes.length || 1;
   const shown = finished ? scenes.length : Math.min(state.index + 1, scenes.length);
   const percent = finished ? 100 : Math.round((shown / total) * 100);
   if (progressFill) progressFill.style.width = percent + '%';
   if (progressPercent) progressPercent.textContent = percent + '%';
   if (progressLabel) {
     progressLabel.textContent = finished
       ? '시나리오 완료'
       : ready
       ? `장면 ${shown} / ${scenes.length}`
       : '분석 준비';
   }
   toggle.textContent = state.playing ? '일시정지' : '계속';
   toggle.disabled = finished;
   root.querySelector('[data-back]').disabled = state.index <= 0;
   root.querySelector('[data-next]').disabled = finished;
   root.querySelector('[data-result]').disabled = finished;
   schedule();
 }
 function pause() {state.playing = false; paint();}
 toggle.addEventListener('click',() => {state.playing = !state.playing; paint();});
 root.querySelector('[data-back]').addEventListener('click',() => {state.index=Math.max(0,state.index-1);pause();});
 root.querySelector('[data-next]').addEventListener('click',() => {state.index=Math.min(scenes.length,state.index+1);pause();});
 root.querySelector('[data-result]').addEventListener('click',() => {state.index=scenes.length;pause();});
 root.querySelector('[data-replay]').addEventListener('click',() => {state.index=0;state.playing=true;paint();});
 const visibility = () => {if (document.hidden) pause();};
 const manual = e => {if (!root.contains(e.target)) pause();};
 const keyboard = e => {if (['PageUp','PageDown','Home','End','ArrowUp','ArrowDown'].includes(e.key)) pause();};
 document.addEventListener('visibilitychange',visibility);
 document.addEventListener('wheel',pause,{passive:true});
 document.addEventListener('touchmove',pause,{passive:true});
 document.addEventListener('click',manual);
 document.addEventListener('keydown',keyboard);
 state.cleanup = () => {
   stopTimer();
   document.removeEventListener('visibilitychange',visibility);
   document.removeEventListener('wheel',pause);
   document.removeEventListener('touchmove',pause);
   document.removeEventListener('click',manual);
   document.removeEventListener('keydown',keyboard);
 };
 if (ready && !state.started) {
   state.started=true;
   state.index=0;
   state.playing=!document.hidden && scenes.length > 0;
 }
 paint();
 if (ready && !state.moved) {
   state.moved=true;
   requestAnimationFrame(() => {
     if (!root.isConnected) {state.moved=false; return;}
     root.scrollIntoView({block:'start',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
     root.querySelector('h3').focus({preventScroll:true});
   });
 }
})();
