// Assigned once by the dev server config; never auto-switches after an API failure.
export const runtime={mode:'api'};
export const isFixture=()=>runtime.mode==='fixture';
