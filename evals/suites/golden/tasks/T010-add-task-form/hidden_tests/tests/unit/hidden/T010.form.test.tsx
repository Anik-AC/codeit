/** @vitest-environment jsdom */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../../../src/client/App.tsx";

const existing = { id: 1, title: "Existing", description: null, createdAt: "2026-01-01T00:00:00Z" };

function stubApi(post: (body: unknown) => Response) {
  const calls: { method: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, body });
    return method === "POST" ? post(body) : Response.json([existing]);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("T010 add-task form", () => {
  it("adds a task to the top of the list and clears the form", async () => {
    const calls = stubApi((body) =>
      Response.json({ id: 2, ...(body as object), createdAt: "2026-01-02T00:00:00Z" }, { status: 201 }),
    );
    render(<App />);
    const form = await screen.findByRole("form", { name: "Add a task" });
    const title = within(form).getByLabelText("Title");
    const description = within(form).getByLabelText("Description");
    fireEvent.change(title, { target: { value: "New one" } });
    fireEvent.change(description, { target: { value: "Details" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add task" }));

    expect(await screen.findByText("You have 2 tasks")).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("New one");
    expect(items[1]).toHaveTextContent("Existing");
    expect(title).toHaveValue("");
    expect(description).toHaveValue("");
    const post = calls.find((c) => c.method === "POST");
    expect(post?.body).toMatchObject({ title: "New one", description: "Details" });
  });

  it("disables the button until there is a title", async () => {
    stubApi(() => Response.json({}, { status: 500 }));
    render(<App />);
    const form = await screen.findByRole("form", { name: "Add a task" });
    const button = within(form).getByRole("button", { name: "Add task" });
    expect(button).toBeDisabled();
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "   " } });
    expect(button).toBeDisabled();
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Real" } });
    expect(button).toBeEnabled();
  });

  it("shows the API's error and keeps the input", async () => {
    stubApi(() => Response.json({ error: "title must be at most 200 characters" }, { status: 400 }));
    render(<App />);
    const form = await screen.findByRole("form", { name: "Add a task" });
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Too long" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add task" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("title must be at most 200 characters");
    expect(within(form).getByLabelText("Title")).toHaveValue("Too long");
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
  });
});
