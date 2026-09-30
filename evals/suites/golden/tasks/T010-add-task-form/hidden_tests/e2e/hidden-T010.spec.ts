import { expect, test } from "@playwright/test";

test("T010: a task added from the page appears at the top", async ({ page }) => {
  await page.goto("/");
  const form = page.getByRole("form", { name: "Add a task" });
  await form.getByLabel("Title").fill(`Buy milk ${Date.now()}`);
  await form.getByRole("button", { name: "Add task" }).click();
  const first = page.getByRole("list", { name: "Tasks" }).getByRole("listitem").first();
  await expect(first).toContainText("Buy milk");
  await expect(form.getByLabel("Title")).toHaveValue("");
});
