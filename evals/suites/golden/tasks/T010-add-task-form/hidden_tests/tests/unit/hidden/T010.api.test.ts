import request from "supertest";
import { describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { openDb, seed } from "../../../src/server/db.ts";

describe("T010 POST /api/tasks", () => {
  it("creates a task that is first in the list", async () => {
    const db = openDb(":memory:");
    seed(db);
    const app = createApp(db);
    const res = await request(app).post("/api/tasks").send({ title: " Buy milk ", description: " 2 l " });
    expect(res.status).toBe(201);
    expect(res.body).toMatchObject({ id: expect.any(Number), title: "Buy milk", description: "2 l" });
    expect(typeof res.body.createdAt).toBe("string");
    const list = await request(app).get("/api/tasks");
    expect(list.body[0]).toEqual(res.body);
  });

  it("stores a blank description as null", async () => {
    const res = await request(createApp(openDb(":memory:"))).post("/api/tasks").send({ title: "T", description: "" });
    expect(res.body.description).toBeNull();
  });

  it.each([{}, { title: "  " }])("requires a title (%j)", async (body) => {
    const res = await request(createApp(openDb(":memory:"))).post("/api/tasks").send(body);
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "title is required" });
  });

  it("limits the title length", async () => {
    const res = await request(createApp(openDb(":memory:"))).post("/api/tasks").send({ title: "x".repeat(201) });
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "title must be at most 200 characters" });
  });
});
