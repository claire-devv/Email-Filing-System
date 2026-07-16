// Fetch a file blob and open it in a new browser tab — used instead of an in-app
// preview pane (the sandboxed preview iframe can't load these URLs anyway).
export async function openBlobInNewTab(loadBlob) {
  const blob = await loadBlob()
  const url = URL.createObjectURL(blob)
  window.open(url, '_blank', 'noopener')
  setTimeout(() => URL.revokeObjectURL(url), 30000)
}
