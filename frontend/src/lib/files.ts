export type FileEntry = { path: string; size: number };

export type DirNode = { name: string; path: string; dirs: DirNode[]; files: FileEntry[] };

/** Build a VS Code-style folder tree from flat workdir-relative paths. */
export function buildTree(files: FileEntry[]): DirNode {
  const root: DirNode = { name: '', path: '', dirs: [], files: [] };
  const byPath = new Map<string, DirNode>([['', root]]);
  for (const f of files) {
    const parts = f.path.split('/');
    let cur = root;
    let acc = '';
    for (let i = 0; i < parts.length - 1; i++) {
      acc = acc ? `${acc}/${parts[i]}` : parts[i];
      let next = byPath.get(acc);
      if (!next) {
        next = { name: parts[i], path: acc, dirs: [], files: [] };
        byPath.set(acc, next);
        cur.dirs.push(next);
      }
      cur = next;
    }
    cur.files.push(f);
  }
  const sortNode = (n: DirNode) => {
    n.dirs.sort((a, b) => a.name.localeCompare(b.name));
    n.files.sort((a, b) => a.path.localeCompare(b.path));
    n.dirs.forEach(sortNode);
  };
  sortNode(root);
  return root;
}

/** Ancestor dir paths of a file, e.g. a/b/c.py -> ['a', 'a/b']. */
export function parentDirs(path: string): string[] {
  const parts = path.split('/').slice(0, -1);
  const out: string[] = [];
  let acc = '';
  for (const p of parts) {
    acc = acc ? `${acc}/${p}` : p;
    out.push(acc);
  }
  return out;
}
