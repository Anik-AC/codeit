import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import request from "supertest";
import { beforeEach, describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { type Db, openDb, seed } from "../../../src/server/db.ts";

describe("T007 done flag", () => {
  let db: Db;

  beforeEach(() => {
    db = openDb(":memory:");
    seed(db);
  });

  it("lists every task as not done at first", async () => {
    const res = await request(createApp(db)).get("/api/tasks");
    expect(res.body.map((t: { done: unknown }) => t.done)).toEqual([false, false, false]);
  });

  it("toggles a task done and back", async () => {
    const app = createApp(db);
    const on = await request(app).post("/api/tasks/1/toggle");
    expect(on.status).toBe(200);
    expect(on.body).toMatchObject({ id: 1, title: "Write the project plan", done: true });
    const off = await request(app).post("/api/tasks/1/toggle");
    expect(off.body.done).toBe(false);
  });

  it("shows the done state in the list", async () => {
    const app = createApp(db);
    await request(app).post("/api/tasks/2/toggle");
    const list = await request(app).get("/api/tasks");
    const done = Object.fromEntries(list.body.map((t: { id: number; done: boolean }) => [t.id, t.done]));
    expect(done).toEqual({ 1: false, 2: true, 3: false });
  });

  it("returns 404 for an unknown task", async () => {
    const res = await request(createApp(db)).post("/api/tasks/42/toggle");
    expect(res.status).toBe(404);
    expect(res.body).toEqual({ error: "Task not found" });
  });

  it("returns 400 for an invalid id", async () => {
    const res = await request(createApp(db)).post("/api/tasks/x/toggle");
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "Invalid task id" });
  });

  it("adds the column to a database from before the change", async () => {
    const dir = mkdtempSync(join(tmpdir(), "t007-"));
    const path = join(dir, "old.db");
    try {
      const old = new DatabaseSync(path);
      old.exec(`CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))`);
      old.exec("PRAGMA user_version = 1");
      old.exec("INSERT INTO tasks (title) VALUES ('From before')");
      old.close();
      const res = await request(createApp(openDb(path))).get("/api/tasks");
      expect(res.body).toEqual([expect.objectContaining({ title: "From before", done: false })]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
