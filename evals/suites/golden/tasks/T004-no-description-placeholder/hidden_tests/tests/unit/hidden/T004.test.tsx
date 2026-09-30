/** @vitest-environment jsdom */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TaskList } from "../../../src/client/TaskList.tsx";

const at = "2026-01-01T00:00:00Z";

describe("T004 description placeholder", () => {
  it("shows the placeholder for a null description", () => {
    render(<TaskList tasks={[{ id: 1, title: "Bare", description: null, createdAt: at }]} />);
    const placeholder = within(screen.getByRole("listitem")).getByText("No description");
    expect(placeholder).toHaveClass("muted");
  });

  it("shows the placeholder for a blank description", () => {
    render(<TaskList tasks={[{ id: 1, title: "Blank", description: "   ", createdAt: at }]} />);
    expect(within(screen.getByRole("listitem")).getByText("No description")).toBeInTheDocument();
  });

  it("shows real descriptions without the placeholder", () => {
    render(<TaskList tasks={[{ id: 1, title: "Full", description: "Some details", createdAt: at }]} />);
    const item = screen.getByRole("listitem");
    expect(within(item).getByText("Some details")).toBeInTheDocument();
    expect(within(item).queryByText("No description")).toBeNull();
  });

  it("puts the placeholder only on the tasks that need it", () => {
    render(
      <TaskList
        tasks={[
          { id: 1, title: "A", description: "Has one", createdAt: at },
          { id: 2, title: "B", description: null, createdAt: at },
        ]}
      />,
    );
    expect(screen.getAllByText("No description")).toHaveLength(1);
  });
});
