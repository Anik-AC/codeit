/** @vitest-environment jsdom */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../../../src/client/App.tsx";

const tasks = [
  { id: 2, title: "Set up CI", description: null, createdAt: "2026-01-01T09:01:00Z", done: false },
  { id: 1, title: "Write the project plan", description: null, createdAt: "2026-01-01T09:00:00Z", done: true },
];

function stubApi(put: (url: string, body: { done: boolean }) => Response) {
  const puts: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body)) as { done: boolean };
        puts.push({ url, body });
        return put(url, body);
      }
      return Response.json(tasks);
    }),
  );
  return puts;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("T011 done checkboxes", () => {
  it("shows done state, the done class and the count", async () => {
    stubApi(() => Response.json({}, { status: 500 }));
    render(<App />);
    expect(await screen.findByRole("checkbox", { name: "Mark Write the project plan as done" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Mark Set up CI as done" })).not.toBeChecked();
    expect(screen.getByText("Write the project plan")).toHaveClass("done");
    expect(screen.getByText("Set up CI")).not.toHaveClass("done");
    expect(screen.getByText("You have 2 tasks, 1 done")).toBeInTheDocument();
  });

  it("marks a task done through the API", async () => {
    const puts = stubApi((_url, body) => Response.json({ ...tasks[0], done: body.done }));
    render(<App />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Mark Set up CI as done" }));
    expect(await screen.findByText("You have 2 tasks, 2 done")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Mark Set up CI as done" })).toBeChecked();
    expect(screen.getByText("Set up CI")).toHaveClass("done");
    expect(puts).toEqual([{ url: "/api/tasks/2/done", body: { done: true } }]);
  });

  it("rolls back and explains when the API fails", async () => {
    stubApi(() => Response.json({ error: "Internal server error" }, { status: 500 }));
    render(<App />);
    const box = await screen.findByRole("checkbox", { name: "Mark Write the project plan as done" });
    fireEvent.click(box);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not update the task");
    expect(box).toBeChecked();
    expect(screen.getByText("You have 2 tasks, 1 done")).toBeInTheDocument();
  });

  it("keeps the plain count when nothing is done", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json([{ ...tasks[0] }])));
    render(<App />);
    expect(await screen.findByText("You have 1 task")).toBeInTheDocument();
  });
});
