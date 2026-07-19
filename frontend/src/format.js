/** Shared display formatting — one implementation so pages can't drift. */

export function fmtBytes(n) {
  if (n == null || n === 0) return null
  // Base 1000 with KB/MB/GB labels — the SI/IEC meaning of those units, and
  // what macOS Finder, iOS Files, and the native app (ByteCountFormatter.file)
  // all report, so the same document reads the same size everywhere.
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = n
  let i = 0
  while (value >= 1000 && i < units.length - 1) {
    value /= 1000
    i++
  }
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`
}
