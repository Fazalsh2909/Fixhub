import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

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
      googleLoginUrl: vi.fn(() => "/api/auth/google/start?next=%2F"),
      githubLoginUrl: vi.fn(() => "/api/auth/github/start?next=%2F"),
    } as any,
  } as any;
});

const renderWithProviders = () => render(<AuthView onAuth={() => {}} />);

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/");
});

afterEach(() => {
  window.history.replaceState({}, "", "/");
});

describe("AuthView: social providers", () => {
  it("renders Google link to the OAuth start URL when enabled", async () => {
    renderWithProviders();
    const link = await screen.findByRole("link", { name: /Google/i });
    expect(link.getAttribute("href")).toBe("/api/auth/google/start?next=%2F");
  });

  it("renders GitHub link to the OAuth start URL when enabled", async () => {
    renderWithProviders();
    const link = await screen.findByRole("link", { name: /GitHub/i });
    expect(link.getAttribute("href")).toBe("/api/auth/github/start?next=%2F");
  });

  it("renders email/password login tab by default", async () => {
    renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Log in/i })).not.toBeNull()
    );
  });

  it("renders signup tab when selected", async () => {
    renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Sign up/i })).not.toBeNull()
    );
  });

  it("shows loading indicator while providers fetch", async () => {
    (api.authProviders as any) = vi.fn(async () => {
      await new Promise((r) => setTimeout(r, 50));
      return { google: { enabled: true }, github: { enabled: false } };
    });
    renderWithProviders();
    expect(screen.getByText("Checking providers…")).toBeTruthy();
    await waitFor(() =>
      expect(screen.queryByText("Checking providers…")).toBeNull()
    );
  });

  it("hides Google link when backend marks it disabled", async () => {
    (api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: false },
      github: { enabled: true },
    }));
    renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /GitHub/i })).not.toBeNull()
    );
    expect(screen.queryByRole("link", { name: /Google/i })).toBeNull();
  });

  it("hides GitHub link when backend marks it disabled", async () => {
    (api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: true },
      github: { enabled: false },
    }));
    renderWithProviders();
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Google/i })).not.toBeNull()
    );
    expect(screen.queryByRole("link", { name: /GitHub/i })).toBeNull();
  });

  it("hides both links when both providers disabled", async () => {
    (api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: false },
      github: { enabled: false },
    }));
    renderWithProviders();
    await waitFor(() =>
      expect(screen.queryByText("Checking providers…")).toBeNull()
    );
    expect(screen.queryByRole("link", { name: /Google/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /GitHub/i })).toBeNull();
  });

  it("hides both links when the provider fetch fails", async () => {
    (api.authProviders as any) = vi.fn(async () => {
      throw new Error("Network error");
    });
    renderWithProviders();
    await waitFor(() =>
      expect(screen.queryByText("Checking providers…")).toBeNull()
    );
    expect(screen.queryByRole("link", { name: /Google/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /GitHub/i })).toBeNull();
  });
});

describe("AuthView: errors", () => {
  it("shows a friendly message returned by the OAuth callback", async () => {
    window.history.replaceState({}, "", "/?auth_error=state");
    renderWithProviders();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("expired or was already used");
  });

  it("shows the email/password form error", async () => {
    (api.authProviders as any) = vi.fn(async () => ({
      google: { enabled: false },
      github: { enabled: false },
    }));
    (api.login as any).mockRejectedValueOnce(new Error("invalid email or password"));
    renderWithProviders();
    await waitFor(() =>
      expect(screen.queryByText("Checking providers…")).toBeNull()
    );
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "a@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("invalid email or password");
  });
});
