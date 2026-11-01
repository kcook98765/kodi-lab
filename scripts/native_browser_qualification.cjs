'use strict';
// Qualification-only helpers. Product handlers remain responsible for authority.
async function activateHeldButton(evaluate, selector) {
  return evaluate(selector => {
    const button = document.querySelector(selector);
    if (!button || button.disabled) throw Error('Control button unavailable');
    // DOM activation intentionally preserves current keyboard focus and held
    // pointer state until the ordinary product handler performs its handoff.
    button.click();
    return {activated: selector, pointerOrFocusActions: false};
  }, selector);
}
async function closeObservedContext(command, context, now=Date.now, nap=ms=>new Promise(r=>setTimeout(r,ms))) {
  await command('browsingContext.close', {context});
  const acknowledged=now(), deadline=acknowledged+1000;
  while (now()<deadline) {
    const tree=await command('browsingContext.getTree', {}, deadline-now());
    const observed=now();
    if (observed>deadline) throw Error('Context absence observation exceeded one second');
    if (!tree.contexts.some(x=>x.context===context)) return {closed_context:context,close_ack:acknowledged,absence_observed:observed,absence_ms:observed-acknowledged,actual_absence_verified:true};
    await nap(Math.min(25,Math.max(0,deadline-now())));
  }
  throw Error('Closed viewer context still present');
}
module.exports={activateHeldButton,closeObservedContext};
// Choose the integer CSS coordinate inside the requested framebuffer pixel.
// Rounding a fractional canvas edge can dispatch into its preceding pixel.
function framebufferPoint(rect,x,y) { return {x:Math.ceil(rect.x+x),y:Math.ceil(rect.y+y)}; }
module.exports.framebufferPoint=framebufferPoint;
