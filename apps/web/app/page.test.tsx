import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";

import Home from "./page";

test("the home page names the product", () => {
  const html = renderToStaticMarkup(<Home />);

  expect(html).toMatch(/<h1[^>]*>Actio<\/h1>/);
});
