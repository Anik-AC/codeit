import { expect, test } from "@playwright/test";

test("T011: ticking a task changes it through the API and survives a reload", async ({ page }) => {
  await page.goto("/");
  const box = page.getByRole("checkbox", { name: "Mark Set up CI as done" });
  // The server is shared across retries, so start from whatever state it is in.
  const before = await box.isChecked();
  await box.click();
  await expect(box).toBeChecked({ checked: !before });
  await page.reload();
  await expect(page.getByRole("checkbox", { name: "Mark Set up CI as done" })).toBeChecked({ checked: !before });
  if (!before) {
    await expect(page.getByText("You have 3 tasks, 1 done")).toBeVisible();
  }
});
