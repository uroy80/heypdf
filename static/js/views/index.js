// Custom tool views, loaded on demand so the front page stays light. Every other tool uses the generic page.

const VIEWS = {
  'pdf.organize': () => import('./organize.js'),
  'pdf.edit': () => import('./edit.js'),
  'pdf.sign': () => import('./sign.js'),
  'pdf.redact': () => import('./redact.js'),
  'pdf.crop': () => import('./crop.js'),
  'pdf.forms': () => import('./forms.js'),
  'pdf.compare': () => import('./compare.js'),
  'pdf.scan': () => import('./scan.js'),
  'pdf.summarize': () => import('./summarize.js'),
  'pdf.translate': () => import('./translate.js'),
};

export const hasView = (toolId) => Object.hasOwn(VIEWS, toolId);

export async function loadView(toolId) {
  return hasView(toolId) ? VIEWS[toolId]() : null;
}
