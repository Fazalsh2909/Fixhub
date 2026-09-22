import AgentPanel from '../AgentPanel';
import Terminal from '../Terminal';
import EngineerPanel from '../engineer/EngineerPanel';
import ChatPanel from '../chat/ChatPanel';
import type { TaskDetail } from '../../lib/tasks';

export type RightTab = 'chat' | 'terminal' | 'engineer' | 'session';

export default function RightPanel({
  tab,
  onTab,
  detail,
  running,
  repo,
  selectedId,
  installationId,
  onRun,
  onWorkdirChanged,
  dark,
}: {
  tab: RightTab;
  onTab: (t: RightTab) => void;
  detail: TaskDetail | null;
  running: boolean;
  repo: string;
  selectedId: number | null;
  installationId: string;
  onRun: () => void;
  onWorkdirChanged: () => void;
  dark: Record<string, string>;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div role="tablist" aria-label="Right panel" style={{ display: 'flex', borderBottom: '1px solid var(--fh-border-subtle)', flexShrink: 0 }}>
        {(['chat', 'terminal', 'engineer', 'session'] as const).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => onTab(t)}
            className="fh-btn"
            style={{
              flex: 1,
              background: 'transparent',
              color: tab === t ? 'var(--fh-text)' : 'var(--fh-muted)',
              border: 0,
              borderBottom: `2px solid ${tab === t ? 'var(--fh-info)' : 'transparent'}`,
              padding: '9px 4px',
              cursor: 'pointer',
              fontSize: 11.5,
              fontWeight: tab === t ? 800 : 500,
              letterSpacing: '0.04em',
            }}
          >
            {t === 'chat' ? '○ Chat' : t === 'terminal' ? '▣ Terminal' : t === 'engineer' ? '✦ AI Engineer' : '◈ Session'}
          </button>
        ))}
        {detail && tab === 'engineer' && (
          <span style={{ alignSelf: 'center', paddingRight: 8 }}>
            <span className="mono" style={{ fontSize: 10, color: 'var(--fh-ok)', border: '1px solid rgba(63,185,80,0.4)', borderRadius: 999, padding: '1px 8px' }}>
              {running ? '● Active' : `● ${detail.state}`}
            </span>
          </span>
        )}
      </div>
      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        {tab === 'chat' && <ChatPanel repo={repo} taskId={selectedId} installationId={installationId} />}
        {tab === 'terminal' && <Terminal repo={repo} dark={dark} />}
        {tab === 'engineer' && (
          <div className="fh-scroll" style={{ flex: 1 }}>
            <EngineerPanel detail={detail} running={running} repo={repo} onRun={onRun} />
          </div>
        )}
        {tab === 'session' && (
          <AgentPanel repo={repo} dark={dark} onWorkdirChanged={onWorkdirChanged} />
        )}
      </div>
    </div>
  );
}
