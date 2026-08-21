import { expect, test } from "@playwright/test";

test("Android autofill diagnostic exposes one native signup credential form", async ({
  page,
}) => {
  await page.goto("/android-autofill-test?mode=signup");

  const form = page.locator("#android-autofill-signup-form");
  const username = form.locator('input[name="username"]');
  const newPassword = form.locator('input[name="new-password"]');
  const confirmPassword = form.locator('input[name="confirm-password"]');

  await expect(form).toHaveAttribute("autocomplete", "on");
  await expect(form.locator('input[type="password"]')).toHaveCount(2);
  await expect(username).toHaveAttribute("autocomplete", "username");
  await expect(newPassword).toHaveAttribute("autocomplete", "new-password");
  await expect(confirmPassword).toHaveAttribute("autocomplete", "new-password");

  await newPassword.evaluate((input: HTMLInputElement) => {
    input.value = "GeneratedAndroidPassword123!";
  });

  await expect(newPassword).toHaveValue("GeneratedAndroidPassword123!");
  await expect(page.getByTestId("android-autofill-event-log")).toContainText(
    "new-password · dom-length-change"
  );
});

test("Android autofill diagnostic exposes canonical change-password fields", async ({
  page,
}) => {
  await page.goto("/android-autofill-test?mode=change");

  const form = page.locator("#android-autofill-change-form");
  const currentPassword = form.locator('input[name="current-password"]');
  const newPassword = form.locator('input[name="new-password"]');
  const confirmPassword = form.locator('input[name="confirm-password"]');

  await expect(form.locator('input[type="password"]')).toHaveCount(3);
  await expect(currentPassword).toHaveAttribute("autocomplete", "current-password");
  await expect(currentPassword).not.toHaveAttribute("minlength", "10");
  await expect(newPassword).toHaveAttribute("autocomplete", "new-password");
  await expect(confirmPassword).toHaveAttribute("autocomplete", "new-password");

  const allInSameForm = await currentPassword.evaluate((input) => {
    const owner = input.closest("form");
    return Boolean(
      owner &&
        document.querySelector('input[name="new-password"]')?.closest("form") === owner &&
        document.querySelector('input[name="confirm-password"]')?.closest("form") === owner &&
        document.querySelector('input[name="username"]')?.closest("form") === owner
    );
  });
  expect(allInSameForm).toBe(true);
});
