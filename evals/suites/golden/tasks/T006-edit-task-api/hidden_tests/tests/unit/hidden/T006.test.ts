import request from "supertest";
import { beforeEach, describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { type Db, openDb, seed } from "../../../src/server/db.ts";

async function task(db: Db, id: number): Promise<Record<string, unknown>> {
  const list = await request(createApp(db)).get("/api/tasks");
  return list.body.find((t: { id: number }) => t.id === id);
}

describe("T006 PATCH /api/tasks/:id", () => {
  let db: Db;

  beforeEach(() => {
    db = openDb(":memory:");
    seed(db);
  });

  it("changes only the title", async () => {
    const before = await task(db, 1);
    const res = await request(createApp(db)).patch("/api/tasks/1").send({ title: "  New title " });
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ ...before, title: "New title" });
    expect(await task(db, 1)).toEqual(res.body);
  });

  it("changes only the description", async () => {
    const before = await task(db, 2);
    const res = await request(createApp(db)).patch("/api/tasks/2").send({ description: "Now described" });
    expect(res.body).toEqual({ ...before, description: "Now described" });
  });

  it("clears the description with null or blank", async () => {
    const res = await request(createApp(db)).patch("/api/tasks/1").send({ description: null });
    expect(res.body.description).toBeNull();
    const blank = await request(createApp(db)).patch("/api/tasks/3").send({ description: "  " });
    expect(blank.body.description).toBeNull();
  });

  it("changes both fields at once and keeps createdAt", async () => {
    const before = await task(db, 3);
    const res = await request(createApp(db)).patch("/api/tasks/3").send({ title: "T", description: "D" });
    expect(res.body).toEqual({ ...before, title: "T", description: "D", createdAt: before.createdAt });
  });

  it("needs something to update", async () => {
    const res = await request(createApp(db)).patch("/api/tasks/1").send({ other: 1 });
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "nothing to update" });
  });

  it.each([
    [{ title: "" }, "title is required"],
    [{ title: 3 }, "title is required"],
    [{ title: "x".repeat(201) }, "title must be at most 200 characters"],
    [{ description: 7 }, "description must be a string"],
  ])("validates %j", async (body, error) => {
    const res = await request(createApp(db)).patch("/api/tasks/1").send(body);
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error });
    expect((await task(db, 1)).title).toBe("Write the project plan");
  });

  it("returns 404 for an unknown task", async () => {
    const res = await request(createApp(db)).patch("/api/tasks/99").send({ title: "x" });
    expect(res.status).toBe(404);
    expect(res.body).toEqual({ error: "Task not found" });
  });

  it("returns 400 for an invalid id", async () => {
    const res = await request(createApp(db)).patch("/api/tasks/nope").send({ title: "x" });
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "Invalid task id" });
  });
});
