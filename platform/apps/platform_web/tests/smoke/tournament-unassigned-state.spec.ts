import { expect, test } from "@playwright/test";

test("authenticated registered player outside published roster sees unassigned state", async ({ page }) => {
  await page.context().addCookies([
    {
      name: "deadlock_platform_session",
      value: "smoke-session",
      url: "http://127.0.0.1:3100"
    },
    {
      name: "team-unassigned-smoke",
      value: "1",
      url: "http://127.0.0.1:3100"
    }
  ]);

  await page.goto("/tournaments/night-veil-open-5");

  await expect(page.getByTestId("tournament-team-unassigned")).toContainText(
    "К сожалению, вы не попали ни в одну команду"
  );
  await expect(page.getByText("Моя команда", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Команды соперников", { exact: true })).toHaveCount(0);
});
