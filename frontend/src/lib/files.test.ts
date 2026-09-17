import { describe, expect, it } from 'vitest';
import { buildTree, parentDirs } from './files';

describe('buildTree', () => {
  it('nests files under folders, dirs-first sorted', () => {
    const tree = buildTree([
      { path: 'b.py', size: 10 },
      { path: 'app/main.py', size: 20 },
      { path: 'app/chat/router.py', size: 30 },
      { path: 'app/api.py', size: 5 },
    ]);
    expect(tree.files.map((f) => f.path)).toEqual(['b.py']);
    expect(tree.dirs.map((d) => d.name)).toEqual(['app']);
    const app = tree.dirs[0];
    expect(app.dirs.map((d) => d.name)).toEqual(['chat']);
    expect(app.files.map((f) => f.path)).toEqual(['app/api.py', 'app/main.py']);
    expect(app.dirs[0].files.map((f) => f.path)).toEqual(['app/chat/router.py']);
  });

  it('handles root-only and empty lists', () => {
    expect(buildTree([]).dirs).toEqual([]);
    const tree = buildTree([{ path: 'a.txt', size: 1 }]);
    expect(tree.files.map((f) => f.path)).toEqual(['a.txt']);
  });
});

describe('parentDirs', () => {
  it('returns ancestor chain', () => {
    expect(parentDirs('a/b/c.py')).toEqual(['a', 'a/b']);
    expect(parentDirs('top.py')).toEqual([]);
  });
});
