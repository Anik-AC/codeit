/** @vitest-environment jsdom */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TaskList } from "../../../src/client/TaskList.tsx";

const tasks = [
  { id: 2, title: "Newer", description: null, createdAt: "2026-03-15T23:30:00.000Z" },
  { id: 1, title: "Older", description: "old one", createdAt: "2026-01-01T09:02:00.000Z" },
];

describe("T002 created dates", () => {
  it("shows each task's creation date in a time element", () => {
    render(<TaskList tasks={tasks} />);
    const [first, second] = screen.getAllByRole("listitem") as [HTMLElement, HTMLElement];
    const newer = within(first).getByText("Mar 15, 2026");
    expect(newer.tagName).toBe("TIME");
    expect(newer).toHaveAttribute("datetime", "2026-03-15T23:30:00.000Z");
    const older = within(second).getByText("Jan 1, 2026");
    expect(older.tagName).toBe("TIME");
    expect(older).toHaveAttribute("datetime", "2026-01-01T09:02:00.000Z");
  });

  it("formats in UTC, not local time", () => {
    render(<TaskList tasks={[{ id: 3, title: "Late", description: null, createdAt: "2026-12-31T23:59:00.000Z" }]} />);
    expect(screen.getByText("Dec 31, 2026")).toBeInTheDocument();
  });

  it("keeps the title and description", () => {
    render(<TaskList tasks={tasks} />);
    expect(screen.getByText("Older")).toBeInTheDocument();
    expect(screen.getByText("old one")).toBeInTheDocument();
  });
});
