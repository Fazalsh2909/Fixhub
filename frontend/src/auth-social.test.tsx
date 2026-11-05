import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import AuthView from "./components/AuthView";
import { api } from "./api";

vi.mock("./api", () => {
  return {
    api: {
      signup: vi.fn(),
      login: vi.fn(),
      logout: vi.fn(),
      me: vi.fn(),
      // authProviders returns enabled state for Google and GitHub
      authProviders: vi.fn(async () => ({
        google: { enabled: true },
        github: { enabled: true },
      })),
      googleLoginUrl: vi.fn(() => "http://localhost/api/auth/google/start"),
      githubLoginUrl: vi.fn(() => "http://localhost/api/auth/github/start"),
    } as any,
  } as any;
});

const renderWithProviders = () => {
  return render(<AuthView onAuth={() => {}} />);
};

describe("AuthView: social providers", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders Google button when enabled", async () => {
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Google/i })).not.toBeNull()
    );
  });

  it("renders GitHub button when enabled", async () => {
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /GitHub/i })).not.toBeNull()
    );
  });

  it("renders email/password login tab by default", async () => {
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Log in/i })).not.toBeNull()
    );
  });

  it("renders signup tab when selected", async () => {
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Sign up/i })).not.toBeNull()
    );
  });

  it("shows loading indicator while providers fetch", async () => {
    ;(api.authProviders as any) = vi.fn(async () => {
      // simulate network delay
      await new Promise((r) => setTimeout(r, 50));
      return { google: { enabled: true }, github: { enabled: false } };
    });
    const user = renderWithProviders();
    expect(screen.getByText("Checking providers…")).toBeTruthy();
    await waitFor(() =>
      expect(screen.queryByText("Checking providers…")).toBeNull()
    );
  });

  it("hides Google button when backend marks it disabled", async () => {
    ;(api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: false },
      github: { enabled: true },
    }));
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /GitHub/i })).not.toBeNull()
    );
    expect(screen.queryByRole("button", { name: /Google/i })).toBeNull();
  });

  it("hides GitHub button when backend marks it disabled", async () => {
    ;(api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: true },
      github: { enabled: false },
    }));
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Google/i })).not.toBeNull()
    );
    expect(screen.queryByRole("button", { name: /GitHub/i })).toBeNull();
  });

  it("hides both buttons when both providers disabled", async () => {
    ;(api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: false },
      github: { enabled: false },
    }));
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Google/i })).toBeNull()
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /GitHub/i })).toBeNull()
    );
  });

  it("shows error state when backend provider fetch fails", async () => {
    ;(api.authProviders as any) = vi.fn(async () => {
      throw new Error("Network error");
    });
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.queryByText("Checking providers…")).toBeNull()
    );
    // providersLoading=false, both false → no provider buttons
    expect(screen.queryByRole("button", { name: /Google/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /GitHub/i })).toBeNull();
  });

  it("calls googleLoginUrl only when Google is enabled", async () => {
    ;(api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: true },
      github: { enabled: false },
    }));
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Google/i })).not.toBeNull()
    );
    fireEvent.click(screen.getByRole("button", { name: /Google/i }));
    await waitFor(() =>
      expect(api.googleLoginUrl).toHaveBeenCalledTimes(1)
    );
    expect(api.githubLoginUrl).not.toHaveBeenCalled();
  });

  it("calls githubLoginUrl only when GitHub is enabled", async () => {
    ;(api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: false },
      github: { enabled: true },
    }));
    const user = renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /GitHub/i })).not.toBeNull()
    );
    fireEvent.click(screen.getByRole("button", { name: /GitHub/i }));
    await waitFor(() =>
      expect(api.githubLoginUrl).toHaveBeenCalledTimes(1)
    );
    expect(api.googleLoginUrl).not.toHaveBeenCalled();
  });
});