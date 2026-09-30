import request from "supertest";
import { beforeEach, describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { type Db, DEMO_TASKS, openDb, seed } from "../../../src/server/db.ts";

const MESSAGE = { error: "limit must be a whole number from 1 to 100" };
const [oldest, middle, newest] = DEMO_TASKS.map((t) => t.title);

describe("T003 GET /api/tasks?limit=", () => {
  let db: Db;

  beforeEach(() => {
    db = openDb(":memory:");
    seed(db);
  });

  it("returns the newest N tasks", async () => {
    const res = await request(createApp(db)).get("/api/tasks?limit=2");
    expect(res.status).toBe(200);
    expect(res.body.map((t: { title: string }) => t.title)).toEqual([newest, middle]);
  });

  it("returns one task for limit=1", async () => {
    const res = await request(createApp(db)).get("/api/tasks?limit=1");
    expect(res.body).toHaveLength(1);
    expect(res.body[0].title).toBe(newest);
  });

  it("returns everything when the limit is larger than the list", async () => {
    const res = await request(createApp(db)).get("/api/tasks?limit=100");
    expect(res.body).toHaveLength(3);
  });

  it("returns everything without a limit, oldest last", async () => {
    const res = await request(createApp(db)).get("/api/tasks");
    expect(res.body).toHaveLength(3);
    expect(res.body[2].title).toBe(oldest);
  });

  it.each(["0", "101", "-1", "abc", "2.5", ""])("rejects limit=%s", async (limit) => {
    const res = await request(createApp(db)).get(`/api/tasks?limit=${limit}`);
    expect(res.status).toBe(400);
    expect(res.body).toEqual(MESSAGE);
  });
});
