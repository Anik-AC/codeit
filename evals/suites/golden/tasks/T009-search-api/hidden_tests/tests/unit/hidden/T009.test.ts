import request from "supertest";
import { beforeEach, describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { type Db, openDb, seed } from "../../../src/server/db.ts";

async function search(db: Db, q: string): Promise<string[]> {
  const res = await request(createApp(db)).get("/api/tasks").query({ q });
  expect(res.status).toBe(200);
  return res.body.map((t: { title: string }) => t.title);
}

describe("T009 GET /api/tasks?q=", () => {
  let db: Db;

  beforeEach(() => {
    db = openDb(":memory:");
    seed(db);
  });

  it("matches the description", async () => {
    expect(await search(db, "agent")).toEqual(["Review open pull requests"]);
  });

  it("matches the title, ignoring case", async () => {
    expect(await search(db, "PLAN")).toEqual(["Write the project plan"]);
  });

  it("keeps newest first for several matches", async () => {
    expect(await search(db, "e")).toEqual(["Review open pull requests", "Set up CI", "Write the project plan"]);
  });

  it("trims, and treats an empty search as no filter", async () => {
    expect(await search(db, "  ci  ")).toEqual(["Set up CI"]);
    expect(await search(db, "")).toHaveLength(3);
    expect(await search(db, "   ")).toHaveLength(3);
  });

  it("treats % and _ literally", async () => {
    expect(await search(db, "%")).toEqual([]);
    expect(await search(db, "_")).toEqual([]);
    db.prepare("INSERT INTO tasks (title) VALUES (?)").run("Grow 10% faster");
    expect(await search(db, "10%")).toEqual(["Grow 10% faster"]);
  });

  it("is safe from SQL injection", async () => {
    expect(await search(db, "' OR 1=1 --")).toEqual([]);
    expect(await search(db, "")).toHaveLength(3);
  });

  it("limits the search to 100 characters", async () => {
    expect(await search(db, "x".repeat(100))).toEqual([]);
    const res = await request(createApp(db)).get("/api/tasks").query({ q: "x".repeat(101) });
    expect(res.status).toBe(400);
    expect(res.body).toEqual({ error: "q must be at most 100 characters" });
  });
});
