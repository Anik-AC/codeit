import request from "supertest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { type Db, openDb, seed } from "../../../src/server/db.ts";

describe("T012 due dates", () => {
  let db: Db;

  beforeEach(() => {
    db = openDb(":memory:");
    seed(db);
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-06-15T23:30:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  async function create(body: object) {
    return request(createApp(db)).post("/api/tasks").send(body);
  }

  it("lists demo tasks with a null due date", async () => {
    const res = await request(createApp(db)).get("/api/tasks");
    expect(res.body.map((t: { dueDate: unknown }) => t.dueDate)).toEqual([null, null, null]);
  });

  it("creates tasks with and without a due date", async () => {
    const dated = await create({ title: " Pay rent ", dueDate: "2026-07-01" });
    expect(dated.status).toBe(201);
    expect(dated.body).toMatchObject({ title: "Pay rent", description: null, dueDate: "2026-07-01" });
    const undated = await create({ title: "Someday", dueDate: null });
    expect(undated.body.dueDate).toBeNull();
    const missing = await create({ title: "Later" });
    expect(missing.body.dueDate).toBeNull();
  });

  it.each(["2026-02-30", "31/05/2026", "2026-5-3", "tomorrow", "2026-13-01", 20260531, ""])(
    "rejects the due date %j",
    async (dueDate) => {
      const res = await create({ title: "T", dueDate });
      expect(res.status).toBe(400);
      expect(res.body).toEqual({ error: "dueDate must be a date like 2026-05-31" });
    },
  );

  it("accepts a leap day", async () => {
    expect((await create({ title: "Leap", dueDate: "2028-02-29" })).status).toBe(201);
  });

  it("validates the title", async () => {
    expect((await create({ dueDate: "2026-07-01" })).body).toEqual({ error: "title is required" });
    expect((await create({ title: "y".repeat(201) })).body).toEqual({
      error: "title must be at most 200 characters",
    });
  });

  it("lists only tasks due before today (UTC)", async () => {
    await create({ title: "Long overdue", dueDate: "2026-01-10" });
    await create({ title: "Yesterday", dueDate: "2026-06-14" });
    await create({ title: "Today", dueDate: "2026-06-15" });
    await create({ title: "Tomorrow", dueDate: "2026-06-16" });
    await create({ title: "No date" });
    const res = await request(createApp(db)).get("/api/tasks?overdue=true");
    expect(res.status).toBe(200);
    expect(res.body.map((t: { title: string }) => t.title).sort()).toEqual(["Long overdue", "Yesterday"]);
  });

  it("rejects other overdue values", async () => {
    for (const value of ["false", "1", "yes", ""]) {
      const res = await request(createApp(db)).get(`/api/tasks?overdue=${value}`);
      expect(res.status).toBe(400);
      expect(res.body).toEqual({ error: "overdue must be true" });
    }
  });
});
