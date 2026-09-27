// 连播在后台进行，用户可能已经离开详情，甚至切到别的番。
// 不靠「当前选中哪一部」，从播放器上报的完整路径反推文件夹和文件名。
export function parseLibraryPath(
  fullPath: string,
  libraryRoot: string,
): { folderName: string; filename: string; relPath: string } | null {
  const normalizedRoot = libraryRoot.replace(/[/\\]$/, "").replace(/\//g, "\\").toLowerCase();
  const normalizedFull = fullPath.replace(/\//g, "\\");
  if (!normalizedFull.toLowerCase().startsWith(normalizedRoot)) return null;
  const rel = normalizedFull.slice(normalizedRoot.length).replace(/^\\/, "");
  const parts = rel.split("\\");
  if (parts.length < 2) return null;
  return {
    folderName: parts[0],
    filename: parts[parts.length - 1],
    relPath: parts.join("/"),
  };
}

export function videoPath(libraryRoot: string, relPath: string): string {
  const cleanRoot = libraryRoot.replace(/[/\\]$/, "");
  return `${cleanRoot}\\${relPath.replace(/\//g, "\\")}`;
}
