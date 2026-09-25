import { describe, expect, it } from 'vitest';
import { pickInstallationForRepo } from './api';

const INSTALLS = [
  { login: 'demo', installation_id: '123' },
  { login: 'Fazalsh2909', installation_id: '160278166' },
  { login: 'demo', installation_id: '1' },
];

describe('pickInstallationForRepo', () => {
  it('picks the installation whose login owns the repo', () => {
    expect(pickInstallationForRepo(INSTALLS, 'Fazalsh2909/Fixhub')).toBe('160278166');
    expect(pickInstallationForRepo(INSTALLS, 'Fazalsh2909/nexus-mcp-intelligence')).toBe(
      '160278166'
    );
  });
  it('matches owner case-insensitively', () => {
    expect(pickInstallationForRepo(INSTALLS, 'fazalsh2909/Fixhub')).toBe('160278166');
  });
  it('returns null when no installation owns the repo', () => {
    expect(pickInstallationForRepo(INSTALLS, 'someone-else/repo')).toBeNull();
    expect(pickInstallationForRepo([], 'Fazalsh2909/Fixhub')).toBeNull();
    expect(pickInstallationForRepo(INSTALLS, '')).toBeNull();
    expect(pickInstallationForRepo(INSTALLS, 'nonslash')).toBeNull();
  });
});
