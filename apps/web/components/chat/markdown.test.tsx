// How a model's words are shown: as the markdown they are written in, and without
// letting them do anything to the page or the reader.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import Markdown from "./markdown";

function show(text: string, { writing = false } = {}) {
  return render(<Markdown text={text} writing={writing} />);
}

/** The formulas on the page, as the LaTeX they were written in. */
function formulas(container: HTMLElement): string[] {
  return [...container.querySelectorAll("annotation")].map(
    (formula) => formula.textContent,
  );
}

describe("markdown becomes what it stands for", () => {
  test("headings, emphasis and lists", () => {
    show(
      [
        "# Bike parts",
        "",
        "The **saddle** is *where* you sit.",
        "",
        "- chain",
        "- pedals",
      ].join("\n"),
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "Bike parts" }),
    ).toBeVisible();
    expect(screen.getByText("saddle")).toHaveClass("font-semibold");
    expect(screen.getByText("where").tagName).toBe("EM");
    expect(
      screen.getAllByRole("listitem").map((item) => item.textContent),
    ).toEqual(["chain", "pedals"]);
    expect(screen.queryByText(/[#*]/)).not.toBeInTheDocument();
  });

  test("a table, with the line breaks models write in its cells", () => {
    const { container } = show(
      [
        "| Part | Road bike |",
        "| --- | --- |",
        "| Tyres | Narrow<br>High pressure |",
      ].join("\n"),
    );

    const table = screen.getByRole("table");
    expect(
      within(table).getByRole("columnheader", { name: "Road bike" }),
    ).toBeVisible();
    const cell = within(table).getByRole("cell", { name: /Narrow/ });
    expect(cell.querySelector("br")).not.toBeNull();
    expect(cell).toHaveTextContent("Narrow High pressure");
    expect(container).not.toHaveTextContent("<br>");
  });

  test("code, with its language and a button that copies it", async () => {
    // Setting up the user also gives the simulated page a clipboard.
    const user = userEvent.setup();
    const { container } = show(
      ["```python", "print('saddle')", "```"].join("\n"),
    );

    expect(container.querySelector("pre")).toHaveTextContent("print('saddle')");
    expect(screen.getByText("python")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Copy Code" }));

    expect(await window.navigator.clipboard.readText()).toBe(
      "print('saddle')\n",
    );
  });

  test("code between backticks is code, whatever is in it", () => {
    const { container } = show("Run `echo $$ <br> **now**` first.");

    expect(container.querySelector("code")).toHaveTextContent(
      "echo $$ <br> **now**",
    );
    expect(formulas(container)).toEqual([]);
  });
});

describe("dollar signs", () => {
  test("a formula between single dollar signs is a formula", () => {
    const { container } = show("Energy is $E = mc^2$, as everyone knows.");

    expect(formulas(container)).toEqual(["E = mc^2"]);
    expect(container.querySelector(".katex-display")).toBeNull();
    expect(container).not.toHaveTextContent("$");
  });

  test.each([
    "A coffee costs $4 and a cake costs $6.",
    "Somewhere between $5-$10.",
    "From $20,000 to $30,000 a year.",
    "The total is **$14**.",
  ])("money stays money: %s", (sentence) => {
    const { container } = show(sentence);

    expect(formulas(container)).toEqual([]);
    expect(container).toHaveTextContent(sentence.replaceAll("*", ""));
  });

  test("a dollar sign with a backslash before it never ends a formula", () => {
    // What a model writes when asked what two coffees cost.
    const { container } = show(
      [
        "Two coffees: $4 \\times 2 = \\$8$, and a cake: $\\$6$.",
        "",
        "That is **\\$14**, not $9 or $10. In code: `echo \\$HOME`.",
      ].join("\n"),
    );

    expect(formulas(container)).toEqual(["4 \\times 2 = \\$8", "\\$6"]);
    expect(container).toHaveTextContent(
      "That is $14, not $9 or $10. In code: echo \\$HOME.",
    );
  });

  test("a backslash before a backslash says nothing about a dollar sign", () => {
    const { container } = show("A backslash \\\\$x + 1$ and no more.");

    expect(formulas(container)).toEqual(["x + 1"]);
    expect(container).toHaveTextContent("A backslash \\");
  });

  test("a formula between double dollar signs stands on its own line", () => {
    const { container } = show(
      [
        "It is stated as:",
        "$$x = \\frac{-b}{2a}$$",
        "",
        "And as a block:",
        "",
        "$$",
        "a^2 + b^2 = c^2",
        "$$",
      ].join("\n"),
    );

    expect(formulas(container)).toEqual([
      "x = \\frac{-b}{2a}",
      "a^2 + b^2 = c^2",
    ]);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(2);
  });

  test("a formula that begins on the line of its dollar signs is whole", () => {
    const { container } = show(["$$x = \\frac{a}{b}", "+ c", "$$"].join("\n"));

    expect(formulas(container)).toEqual(["x = \\frac{a}{b}\n+ c"]);
  });

  test.each([
    ["an answer that was stopped", false],
    ["an answer that is arriving", true],
  ])("a formula that was cut off is still shown: %s", (_, writing) => {
    // The second formula: the library completes the first one differently.
    const { container } = show(
      ["$$a = 1$$", "", "Then:", "$$x = \\frac{b^2}{"].join("\n"),
      { writing },
    );

    expect(container).toHaveTextContent("x = \\frac{b^2}{");
  });
});

describe("an answer can't do anything to the page", () => {
  test("HTML between words is shown as text", () => {
    const { container } = show(
      'Use the <b>bold</b> tag, or <br>. <img src="x" onerror="window.pwned = 1"> <script>window.pwned = 2</script>',
    );

    expect(container).toHaveTextContent(
      'Use the <b>bold</b> tag, or <br>. <img src="x" onerror="window.pwned = 1"> <script>window.pwned = 2</script>',
    );
    expect(container.querySelector("b, br, img, script")).toBeNull();
    expect(window).not.toHaveProperty("pwned");
  });

  test("HTML on its own lines is shown as code", () => {
    const { container } = show(
      [
        "A page:",
        "",
        '<div style="position: fixed; inset: 0">',
        "  <iframe src='https://example.org/'></iframe>",
        "</div>",
        "",
        "<style>body { display: none }</style>",
      ].join("\n"),
    );

    expect(
      container.querySelector("div[style*=fixed], iframe, style"),
    ).toBeNull();
    const [page, style] = [...container.querySelectorAll("pre")];
    expect(page).toHaveTextContent('<div style="position: fixed; inset: 0">');
    expect(page).toHaveTextContent(
      "<iframe src='https://example.org/'></iframe>",
    );
    expect(style).toHaveTextContent("<style>body { display: none }</style>");
  });

  test("a line break on its own line is only space", () => {
    const { container } = show(["Above", "", "<br>", "", "Below"].join("\n"));

    expect(container).toHaveTextContent("AboveBelow");
    expect(container.querySelector("pre")).toBeNull();
  });

  test("a picture is not fetched", () => {
    const { container } = show(
      [
        "![a chart](https://example.org/pixel.png?said=everything)",
        "",
        "![a dot](data:image/png;base64,iVBORw0KGgo=)",
        "",
        "![](//example.org/pixel.png)",
      ].join("\n"),
    );

    expect(container.querySelector("img, picture, source")).toBeNull();
    expect(container).not.toHaveTextContent("example.org");
    expect(screen.getByText("[Image blocked: a chart]")).toBeVisible();
    expect(screen.getByText("[Image blocked: a dot]")).toBeVisible();
    expect(screen.getByText("[Image blocked: No description]")).toBeVisible();
  });

  test("a formula can't hold a link or a picture", () => {
    // LaTeX has commands for both. The formula renderer obeys them only when told
    // that what it renders can be trusted.
    const { container } = show(
      "$\\href{https://evil.example/}{x}$ and $\\includegraphics{https://evil.example/pixel.png}$",
    );

    expect(formulas(container)).toHaveLength(2);
    expect(container.querySelector("a, img, button")).toBeNull();
  });

  test("a link opens only after its address was shown", async () => {
    const opened = vi.spyOn(window, "open").mockReturnValue(null);
    const user = userEvent.setup();
    show("Read [the bank's page](https://evil.example/login).");

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "the bank's page" }));

    expect(opened).not.toHaveBeenCalled();
    expect(screen.getByText("https://evil.example/login")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Open link" }));

    expect(opened).toHaveBeenCalledWith(
      "https://evil.example/login",
      "_blank",
      "noreferrer",
    );
  });

  test("an address written out is a link too, and so is an email address", () => {
    show(
      "See https://docs.python.org/3/ or [write](mailto:someone@example.org).",
    );

    expect(
      screen.getByRole("button", { name: "https://docs.python.org/3/" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "write" })).toBeVisible();
  });

  test.each([
    ["a script", "javascript:window.pwned=3"],
    ["a file", "file:///etc/passwd"],
    ["a program", "vscode://file/etc/passwd"],
    ["a telephone", "tel:+905550000000"],
    ["data", "data:text/html,<script>window.pwned=4</script>"],
  ])("a link to %s is not a link", (words, address) => {
    const { container } = show(`Open [${words}](${address}) now.`);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(container).toHaveTextContent(`Open ${words} [blocked] now.`);
  });
});

describe("while an answer arrives", () => {
  test("what is half-written is shown as what it is becoming", () => {
    const { container } = show("The **sadd", { writing: true });

    expect(screen.getByText("sadd")).toHaveClass("font-semibold");
    expect(container).not.toHaveTextContent("*");
  });

  test("a link that is half-written is its words", () => {
    const { container } = show("See [the docs](https://docs.pyth", {
      writing: true,
    });

    expect(container).toHaveTextContent(/^See the docs$/);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  test("code that is half-written is code", () => {
    const { container } = show(["```python", "print('sad"].join("\n"), {
      writing: true,
    });

    expect(container.querySelector("pre")).toHaveTextContent("print('sad");
    expect(container).not.toHaveTextContent("```");
  });

  test("an answer that was stopped half-way through a word in bold", () => {
    // A stored answer can end anywhere: it is kept as far as it got.
    show("The **sadd");

    expect(screen.getByText("sadd")).toHaveClass("font-semibold");
  });
});

describe("diagrams", () => {
  // The library draws a diagram once it is on screen, which a simulated page can't
  // tell. Say it is.
  function onScreen() {
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(
          private readonly seen: (entries: object[], observer: object) => void,
        ) {}
        observe() {
          this.seen([{ isIntersecting: true }], this);
        }
        takeRecords() {
          return [];
        }
        disconnect() {
          // Nothing to let go of.
        }
      },
    );
  }

  const DIAGRAM = ["```mermaid", "flowchart TD", "  A --> B", "```"].join("\n");

  test("a diagram is drawn", async () => {
    onScreen();
    vi.doMock("@streamdown/mermaid", () => ({
      mermaid: {
        getMermaid: (config: { theme: string }) => ({
          render: (_id: string, source: string) =>
            Promise.resolve({
              svg: `<svg viewBox="0 0 10 10"><title>${config.theme}: ${source}</title></svg>`,
            }),
        }),
      },
    }));
    const { container } = show(DIAGRAM);

    await waitFor(() => {
      expect(container.querySelector("svg title")).toHaveTextContent(
        "default: flowchart TD A --> B",
      );
    });
    vi.doUnmock("@streamdown/mermaid");
  });

  test("a diagram that is still arriving is not drawn yet", async () => {
    onScreen();
    const draw = vi.fn(() => Promise.resolve({ svg: "<svg></svg>" }));
    vi.doMock("@streamdown/mermaid", () => ({
      mermaid: { getMermaid: () => ({ render: draw }) },
    }));
    const { container } = show(
      ["```mermaid", "flowchart TD", "  A --"].join("\n"),
      { writing: true },
    );

    // Half a diagram is a mistake in a diagram: it would flash as one.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(draw).not.toHaveBeenCalled();
    expect(container.querySelector("svg title")).toBeNull();
    vi.doUnmock("@streamdown/mermaid");
  });

  test("a diagram that can't be drawn is shown as it was written", async () => {
    onScreen();
    vi.doMock("@streamdown/mermaid", () => ({
      mermaid: {
        getMermaid: () => ({
          render: () => Promise.reject(new Error("Parse error on line 2")),
        }),
      },
    }));
    const { container } = show(DIAGRAM);

    expect(
      await screen.findByText("This diagram can't be drawn as it was written."),
    ).toBeVisible();
    expect(container.querySelector("pre")).toHaveTextContent(
      "flowchart TD A --> B",
    );
    vi.doUnmock("@streamdown/mermaid");
  });
});
