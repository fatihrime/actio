// The app's icon, as a browser finds it: by the links in the page's head, and at the
// address a browser asks for when it has read no page (ADR 0015, revision of
// 2026-11-21).
import { expect, test } from "./chat";

test("the app has an icon, also where a browser looks for one by itself", async ({
  chat,
  page,
}) => {
  const plain = await page.request.get("/favicon.ico");
  expect(plain.status()).toBe(200);
  expect(plain.headers()["content-type"]).toMatch(/^image\//);

  await chat.open();
  const links = page.locator('link[rel="icon"]');
  await expect(links).toHaveCount(2);
  for (const link of await links.all()) {
    const href = (await link.getAttribute("href")) ?? "";
    const response = await page.request.get(href);
    expect(response.status(), href).toBe(200);
    expect(response.headers()["content-type"]?.split(";")[0], href).toBe(
      await link.getAttribute("type"),
    );
  }
});
