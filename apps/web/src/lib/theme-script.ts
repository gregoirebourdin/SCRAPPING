export const THEME_KEY = "scout-theme";

/** Runs before paint (inlined in <head>) so the page never flashes the wrong theme. */
export const THEME_BOOTSTRAP = `(function(){try{var p=localStorage.getItem("${THEME_KEY}")||"dark";var t=p==="system"?(matchMedia("(prefers-color-scheme: light)").matches?"light":"dark"):p;document.documentElement.dataset.theme=t;document.documentElement.style.colorScheme=t}catch(e){}})();`;
