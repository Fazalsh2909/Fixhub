import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const USER = {
  id: 7,
  email: "ada@example.com",
  display_name: "Ada Lovelace",
  avatar_url: "",
};

const INSTALL_URL = "https://github.com/apps/fixhub/installations/new";
type Handler = (init?: RequestInit, url?: URL) => Response | Promise<Response>;
type Routes = Record<string, Handler>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(routes: Routes) {
  const calls: Array<{ method: string; url: string; body?: string }> = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const url = new URL(raw, "http://localhost:5173");
    const method = (init?.method || "GET").toUpperCase();
    calls.push({
      method,
      url: `${url.pathname}${url.search}`,
      body: typeof init?.body === "string" ? init.body : undefined,
    });
    const handler = routes[`${method} ${url.pathname}`];
    if (!handler) return json({ detail: `unmocked ${method} ${url.pathname}` }, 404);
    return handler(init, url);
  });
  vi.stubGlobal("fetch", mock);
  return { mock, calls };
}

function baseRoutes(
  repos: Handler = () => json([]),
  overrides: Routes = {},
): Routes {
  return {
    "GET /api/auth/me": () => json(USER),
    "GET /api/tasks": () => json([]),
    "GET /api/queue/health": () => json({ ok: true, workers: 1 }),
    "GET /api/github/config": () =>
      json({
        install_url: INSTALL_URL,
        oauth_enabled: true,
        start_url: "/api/github/install/start",
      }),
    "GET /api/github/repos": repos,
    "GET /api/llm/providers": () => json([]),
    "GET /api/llm/credentials": () => json([]),
    "GET /api/auth/providers": () =>
      json({
        google: { enabled: true },
        github: { enabled: true },
      }),
    "POST /api/auth/logout": () => json({ ok: true }),
    ...overrides,
  };
}

async function renderAuthed(routes: Routes) {
  const net = installFetch(routes);
  render(<App />);
  await screen.findByRole("button", { name: "Log out" });
  return net;
}

async function openRepositories() {
  fireEvent.click(screen.getByRole("button", { name: "Repositories" }));
  await screen.findByText("Repository access");
}

const groupWith = (repos: Array<Record<string, unknown>>) => [
  {
    installation_id: "123",
    account: "acme",
    type: "User",
    repos,
  },
];

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("GitHub connection", () => {
  it("shows Connect GitHub when GitHub is not authorized", async () => {
    await renderAuthed(
      baseRoutes(() =>
        json({
          error: "connect your GitHub account first",
          github_authorization_required: true,
          start_url: "/api/github/install/start",
        }, 400),
      ),
    );
    await openRepositories();

    expect(await screen.findByText("Not connected")).not.toBeNull();
    const card = document.querySelector(".github-card") as HTMLElement;
    const link = within(card).getByRole("link", { name: "Connect GitHub" });
    expect(link.getAttribute("href")).toBe(
      "/api/github/install/start?next=%2F%3Fview%3Drepositories",
    );
    expect(screen.queryByText(/No GitHub installations/i)).toBeNull();
  });

  it("shows a persistent Connect GitHub action in the header", async () => {
    await renderAuthed(
      baseRoutes(() =>
        json({
          error: "connect your GitHub account first",
          github_authorization_required: true,
          start_url: "/api/github/install/start",
        }, 400),
      ),
    );

    const header = document.querySelector(".app-header") as HTMLElement;
    const link = await within(header).findByRole("link", {
      name: "Connect GitHub",
    });
    expect(link.getAttribute("href")).toBe(
      "/api/github/install/start?next=%2F%3Fview%3Drepositories",
    );
  });

  it("disables the header Connect action when OAuth is not configured", async () => {
    await renderAuthed(
      baseRoutes(
        () =>
          json({
            error: "connect your GitHub account first",
            github_authorization_required: true,
            start_url: "/api/github/install/start",
          }, 400),
        {
          "GET /api/github/config": () =>
            json({
              install_url: "",
              oauth_enabled: false,
              start_url: "/api/github/install/start",
            }),
        },
      ),
    );

    const header = document.querySelector(".app-header") as HTMLElement;
    const button = await within(header).findByRole("button", {
      name: "Connect GitHub",
    });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the connected state in the header once GitHub is ready", async () => {
    await renderAuthed(
      baseRoutes(() =>
        json(
          groupWith([
            {
              github_full_name: "acme/connected",
              private: false,
              default_branch: "main",
              connected: true,
              installation_id: "123",
            },
          ]),
        ),
      ),
    );

    const header = document.querySelector(".app-header") as HTMLElement;
    expect(
      await within(header).findByRole("button", { name: "GitHub connected" }),
    ).not.toBeNull();
    expect(
      within(header).queryByRole("link", { name: "Connect GitHub" }),
    ).toBeNull();
  });

  it("offers the App install action when authorized with zero installations", async () => {
    await renderAuthed(baseRoutes(() => json([])));
    await openRepositories();

    expect(await screen.findByText("App not installed")).not.toBeNull();
    const card = document.querySelector(".github-card") as HTMLElement;
    const link = within(card).getByRole("link", { name: /Install GitHub App/i });
    expect(link.getAttribute("href")).toBe(INSTALL_URL);
    expect(
      screen.getByRole("button", { name: "Refresh GitHub connection" }),
    ).not.toBeNull();
  });

  it("explains the missing install URL without guessing an App slug", async () => {
    await renderAuthed(
      baseRoutes(() => json([]), {
        "GET /api/github/config": () =>
          json({
            install_url: "",
            oauth_enabled: true,
            start_url: "/api/github/install/start",
          }),
      }),
    );
    await openRepositories();

    expect(await screen.findByText(/GITHUB_APP_SLUG/)).not.toBeNull();
    expect(
      screen.queryByRole("link", { name: /Install GitHub App/i }),
    ).toBeNull();
  });

  it("refreshes after a successful callback and cleans the URL", async () => {
    window.history.replaceState(
      {},
      "",
      "/?view=repositories&github_connect=done",
    );
    const net = await renderAuthed(baseRoutes(() => json([])));

    expect(
      await screen.findByText(
        "GitHub connected. Repository access is up to date.",
      ),
    ).not.toBeNull();
    await waitFor(() => expect(window.location.search).toBe(""));
    await waitFor(() =>
      expect(
        net.calls.filter(
          (call) => call.method === "GET" && call.url === "/api/github/repos",
        ).length,
      ).toBeGreaterThanOrEqual(2),
    );
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("shows a retry action after a failed callback", async () => {
    window.history.replaceState(
      {},
      "",
      "/?view=repositories&github_connect=error",
    );
    await renderAuthed(baseRoutes(() => json([])));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("GitHub authorization didn’t complete");
    const retry = within(alert).getByRole("link", { name: "Try again" });
    expect(retry.getAttribute("href")).toBe(
      "/api/github/install/start?next=%2F%3Fview%3Drepositories",
    );
    await waitFor(() => expect(window.location.search).toBe(""));
    expect(screen.queryByText("Checking…")).toBeNull();
  });

  it("shows connected/unconnected repositories and connects via the existing endpoint", async () => {
    let connected = false;
    const postBodies: Array<Record<string, unknown>> = [];
    let resolvePost!: (response: Response) => void;
    const postResponse = new Promise<Response>((resolve) => {
      resolvePost = resolve;
    });
    const net = await renderAuthed(
      baseRoutes(
        () =>
          json(
            groupWith([
              {
                github_full_name: "acme/connected",
                private: false,
                default_branch: "main",
                connected: true,
                installation_id: "123",
              },
              {
                github_full_name: "acme/unconnected",
                private: true,
                default_branch: "main",
                connected,
                installation_id: "123",
              },
            ]),
          ),
        {
          "POST /api/repositories/connect": (init) => {
            postBodies.push(
              JSON.parse(String(init?.body)) as Record<string, unknown>,
            );
            return postResponse;
          },
        },
      ),
    );
    await openRepositories();

    expect(
      await screen.findByRole("button", { name: "acme/connected, connected" }),
    ).not.toBeNull();
    expect(
      await screen.findByRole("button", {
        name: "acme/unconnected, not connected",
      }),
    ).not.toBeNull();

    const connectButton = screen.getByRole("button", {
      name: "Connect acme/unconnected",
    });
    fireEvent.click(connectButton);
    fireEvent.click(connectButton);

    await waitFor(() => expect(postBodies.length).toBe(1));
    expect(postBodies[0]).toEqual({
      github_full_name: "acme/unconnected",
      installation_id: "123",
    });
    expect(
      net.calls.some(
        (call) => call.method === "POST" && call.url === "/api/repositories/connect",
      ),
    ).toBe(true);

    connected = true;
    resolvePost(
      json({ id: 2, github_full_name: "acme/unconnected", connected: true }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Connect acme/unconnected" }),
      ).toBeNull(),
    );
    expect(
      screen.getByRole("button", { name: "acme/unconnected, connected" }),
    ).not.toBeNull();
  });

  it("does not render a GitHub API failure as an empty repository list", async () => {
    await renderAuthed(
      baseRoutes(() => json({ error: "github api failed: 502" }, 502)),
    );
    await openRepositories();

    expect(await screen.findByText("Temporarily unavailable")).not.toBeNull();
    expect(
      screen.getByRole("button", {
        name: "Retry loading GitHub repositories",
      }),
    ).not.toBeNull();
    expect(screen.queryByText(/No GitHub installations/i)).toBeNull();
    expect(screen.queryByRole("link", { name: "Connect GitHub" })).toBeNull();
    expect(
      screen.getByText(/does not mean GitHub was disconnected/i),
    ).not.toBeNull();
  });

  it("shows status, accounts and repository counts in Settings", async () => {
    await renderAuthed(
      baseRoutes(() =>
        json(
          groupWith([
            {
              github_full_name: "acme/connected",
              private: false,
              default_branch: "main",
              connected: true,
              installation_id: "123",
            },
            {
              github_full_name: "acme/available",
              private: true,
              default_branch: "main",
              connected: false,
              installation_id: "123",
            },
          ]),
        ),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await screen.findByText("Repository access");

    const card = document.querySelector(".github-card") as HTMLElement;
    expect(card).not.toBeNull();
    expect(await within(card).findByText("acme")).not.toBeNull();
    const stats = card.querySelector(".github-stats") as HTMLElement;
    expect(stats).not.toBeNull();
    expect(within(stats).getByText("Available")).not.toBeNull();
    expect(within(stats).getByText("2")).not.toBeNull();
    expect(within(stats).getByText("Connected")).not.toBeNull();
    expect(within(stats).getByText("1")).not.toBeNull();
    expect(
      within(card).getByRole("link", { name: "Reconnect GitHub" }),
    ).not.toBeNull();
  });
});

describe("FixHub authentication", () => {
  it("keeps signup working", async () => {
    installFetch(
      baseRoutes(() => json([]), {
        "GET /api/auth/me": () => json({ detail: "Not authenticated" }, 401),
        "POST /api/auth/signup": () => json(USER),
      }),
    );
    render(<App />);

    fireEvent.click(await screen.findByRole("tab", { name: "Sign up" }));
    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "Ada Lovelace" },
    });
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "ada@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-horse-123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(
      await screen.findByRole("button", { name: "Log out" }),
    ).not.toBeNull();
  });

  it("keeps login and logout working", async () => {
    const net = installFetch(
      baseRoutes(() => json([]), {
        "GET /api/auth/me": () => json({ detail: "Not authenticated" }, 401),
        "POST /api/auth/login": () => json(USER),
      }),
    );
    render(<App />);

    fireEvent.change(await screen.findByLabelText("Email"), {
      target: { value: "ada@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-horse-123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));

    fireEvent.click(await screen.findByRole("button", { name: "Log out" }));
    expect(await screen.findByRole("tab", { name: "Log in" })).not.toBeNull();
    expect(
      net.calls.some(
        (call) => call.method === "POST" && call.url === "/api/auth/logout",
      ),
    ).toBe(true);
  });
});