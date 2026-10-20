/* Applies a saved light/dark preference before first paint (classic script, no module).
   On the privacy page it also wires the "Forget my choices on this device" button ([data-forget-theme]):
   un-hidden only when this script runs, it removes every key heypdf keeps in local storage. */
(function () {
  var KEYS = ['heypdf-theme', 'heypdf-ai-notice'];
  try {
    var t = window.localStorage.getItem('heypdf-theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) {
    /* storage unavailable (private mode, blocked site data) — follow the system theme */
  }

  function forget(ev) {
    var button = ev.currentTarget;
    try {
      for (var i = 0; i < KEYS.length; i++) window.localStorage.removeItem(KEYS[i]);
    } catch (e) {
      /* nothing stored, or storage blocked: nothing to forget */
    }
    document.documentElement.removeAttribute('data-theme');
    button.textContent = 'Done — this device no longer remembers your choices';
    button.disabled = true;
  }

  function wire() {
    var buttons = document.querySelectorAll('[data-forget-theme]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].hidden = false;
      buttons[i].addEventListener('click', forget);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();
