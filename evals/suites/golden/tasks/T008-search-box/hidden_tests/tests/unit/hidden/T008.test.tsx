/** @vitest-environment jsdom */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../../src/client/App.tsx";

const tasks = [
  { id: 3, title: "Review open pull requests", description: null, createdAt: "2026-01-01T09:02:00Z" },
  { id: 2, title: "Set up CI", description: null, createdAt: "2026-01-01T09:01:00Z" },
  { id: 1, title: "Write the project plan", description: null, createdAt: "2026-01-01T09:00:00Z" },
];

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async () => Response.json(tasks));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function titles(): string[] {
  return screen.queryAllByRole("listitem").map((li) => li.querySelector("strong")?.textContent ?? "");
}

describe("T008 search box", () => {
  it("filters by title, ignoring case", async () => {
    render(<App />);
    const search = await screen.findByLabelText("Search tasks");
    fireEvent.change(search, { target: { value: "PRO" } });
    expect(titles()).toEqual(["Write the project plan"]);
  });

  it("keeps the usual order for several matches", async () => {
    render(<App />);
    fireEvent.change(await screen.findByLabelText("Search tasks"), { target: { value: "e" } });
    expect(titles()).toEqual(["Review open pull requests", "Set up CI", "Write the project plan"]);
    fireEvent.change(screen.getByLabelText("Search tasks"), { target: { value: " set " } });
    expect(titles()).toEqual(["Set up CI"]);
  });

  it("shows every task for an empty search", async () => {
    render(<App />);
    const search = await screen.findByLabelText("Search tasks");
    fireEvent.change(search, { target: { value: "ci" } });
    fireEvent.change(search, { target: { value: "" } });
    expect(titles()).toHaveLength(3);
  });

  it("says when nothing matches", async () => {
    render(<App />);
    fireEvent.change(await screen.findByLabelText("Search tasks"), { target: { value: "zzz" } });
    expect(screen.getByText("No tasks match your search.")).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("keeps the total count and makes no extra requests", async () => {
    render(<App />);
    fireEvent.change(await screen.findByLabelText("Search tasks"), { target: { value: "ci" } });
    expect(screen.getByText("You have 3 tasks")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
