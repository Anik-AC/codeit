import request from "supertest";
import { beforeEach, describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { type Db, openDb, seed } from "../../../src/server/db.ts";

describe("T005 POST /api/tasks", () => {
  let db: Db;

  beforeEach(() => {
    db = openDb(":memory:");
    seed(db);
  });

  it("creates a task and returns it with 201 and a Location", async () => {
    const res = await request(createApp(db)).post("/api/tasks").send({ title: "Buy milk", description: "Two litres" });
    expect(res.status).toBe(201);
    expect(res.body).toMatchObject({ id: expect.any(Number), title: "Buy milk", description: "Two litres" });
    expect(new Date(res.body.createdAt).toISOString()).toBe(res.body.createdAt);
    expect(res.headers.location).toBe(`/api/tasks/${res.body.id}`);
  });

  it("puts the new task first in the list", async () => {
    const app = createApp(db);
    const created = await request(app).post("/api/tasks").send({ title: "Newest" });
    const list = await request(app).get("/api/tasks");
    expect(list.body[0]).toEqual(created.body);
    expect(list.body).toHaveLength(4);
  });

  it("trims and stores a missing description as null", async () => {
    const res = await request(createApp(db)).post("/api/tasks").send({ title: "  Spaced  " });
    expect(res.body.title).toBe("Spaced");
    expect(res.body.description).toBeNull();
  });

  it("stores a blank description as null", async () => {
    const res = await request(createApp(db)).post("/api/tasks").send({ title: "T", description: "   " });
    expect(res.body.description).toBeNull();
  });

  it("accepts an explicit null description", async () => {
    const res = await request(createApp(db)).post("/api/tasks").send({ title: "T", description: null });
    expect(res.status).toBe(201);
    expect(res.body.description).toBeNull();
  });

  it.each([{}, { title: "" }, { title: "   " }, { title: 42 }, { description: "x" }])(
    "requires a title (%j)",
    async (body) => {
      const res = await request(createApp(db)).post("/api/tasks").send(body);
      expect(res.status).toBe(400);
      expect(res.body).toEqual({ error: "title is required" });
    },
  );

  it("limits the title to 200 characters", async () => {
    const ok = await request(createApp(db)).post("/api/tasks").send({ title: "a".repeat(200) });
    expect(ok.status).toBe(201);
    const res = await request(createApp(db)).post("/api/tasks").send({ title: "a".repeat(201) });
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "title must be at most 200 characters" });
  });

  it("rejects a description that is not a string", async () => {
    const res = await request(createApp(db)).post("/api/tasks").send({ title: "T", description: 5 });
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "description must be a string" });
  });

  it("does not store anything on a bad request", async () => {
    const app = createApp(db);
    await request(app).post("/api/tasks").send({ title: "" });
    const list = await request(app).get("/api/tasks");
    expect(list.body).toHaveLength(3);
  });
});
