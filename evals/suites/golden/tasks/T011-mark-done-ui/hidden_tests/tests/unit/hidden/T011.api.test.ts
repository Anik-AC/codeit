import request from "supertest";
import { beforeEach, describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { type Db, openDb, seed } from "../../../src/server/db.ts";

describe("T011 PUT /api/tasks/:id/done", () => {
  let db: Db;

  beforeEach(() => {
    db = openDb(":memory:");
    seed(db);
  });

  it("starts every task not done", async () => {
    const res = await request(createApp(db)).get("/api/tasks");
    expect(res.body.every((t: { done: unknown }) => t.done === false)).toBe(true);
  });

  it("sets and clears done", async () => {
    const app = createApp(db);
    const on = await request(app).put("/api/tasks/2/done").send({ done: true });
    expect(on.status).toBe(200);
    expect(on.body).toMatchObject({ id: 2, title: "Set up CI", done: true });
    const again = await request(app).put("/api/tasks/2/done").send({ done: true });
    expect(again.body.done).toBe(true);
    const off = await request(app).put("/api/tasks/2/done").send({ done: false });
    expect(off.body.done).toBe(false);
  });

  it("shows done in the list", async () => {
    const app = createApp(db);
    await request(app).put("/api/tasks/3/done").send({ done: true });
    const list = await request(app).get("/api/tasks");
    expect(list.body.find((t: { id: number }) => t.id === 3).done).toBe(true);
  });

  it.each([{}, { done: "yes" }, { done: 1 }, { done: null }])("rejects %j", async (body) => {
    const res = await request(createApp(db)).put("/api/tasks/1/done").send(body);
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "done must be true or false" });
  });

  it("returns 404 and 400 for bad ids", async () => {
    const missing = await request(createApp(db)).put("/api/tasks/77/done").send({ done: true });
    expect(missing.status).toBe(404);
    expect(missing.body).toEqual({ error: "Task not found" });
    const invalid = await request(createApp(db)).put("/api/tasks/abc/done").send({ done: true });
    expect(invalid.status).toBe(400);
    expect(invalid.body).toEqual({ error: "Invalid task id" });
  });
});
