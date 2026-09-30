import request from "supertest";
import { beforeEach, describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { type Db, openDb, seed } from "../../../src/server/db.ts";

describe("T001 DELETE /api/tasks/:id", () => {
  let db: Db;

  beforeEach(() => {
    db = openDb(":memory:");
    seed(db);
  });

  it("deletes an existing task with 204 and an empty body", async () => {
    const res = await request(createApp(db)).delete("/api/tasks/2");
    expect(res.status).toBe(204);
    expect(res.text).toBe("");
  });

  it("removes the task from the list", async () => {
    const app = createApp(db);
    await request(app).delete("/api/tasks/2");
    const list = await request(app).get("/api/tasks");
    expect(list.body.map((t: { id: number }) => t.id).sort()).toEqual([1, 3]);
  });

  it("leaves the other tasks alone", async () => {
    const app = createApp(db);
    await request(app).delete("/api/tasks/1");
    const list = await request(app).get("/api/tasks");
    expect(list.body).toHaveLength(2);
  });

  it("returns 404 for a task that does not exist", async () => {
    const res = await request(createApp(db)).delete("/api/tasks/99");
    expect(res.status).toBe(404);
    expect(res.body).toEqual({ error: "Task not found" });
  });

  it("returns 404 the second time a task is deleted", async () => {
    const app = createApp(db);
    await request(app).delete("/api/tasks/3");
    const again = await request(app).delete("/api/tasks/3");
    expect(again.status).toBe(404);
  });

  it.each(["abc", "0", "-1", "1.5"])("returns 400 for the id %s", async (id) => {
    const res = await request(createApp(db)).delete(`/api/tasks/${id}`);
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "Invalid task id" });
  });
});
