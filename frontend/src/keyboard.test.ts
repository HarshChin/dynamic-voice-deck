import { describe, expect, it } from "vitest";

import { activatesOnSpace, isTypingTarget } from "./keyboard";

/**
 * Build a detached element, which is all these predicates look at.
 *
 * @param html - The element's markup.
 * @returns The element.
 */
function element(html: string): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = html;
  return host.firstElementChild as HTMLElement;
}

/**
 * Build an element that reports itself as content-editable.
 *
 * jsdom parses the attribute but never implements `isContentEditable`, which is the property a
 * browser actually sets and the one the predicate reads, so it is defined here directly.
 *
 * @returns The element.
 */
function contentEditable(): HTMLElement {
  const node = element("<div></div>");
  Object.defineProperty(node, "isContentEditable", { value: true });
  return node;
}

describe("isTypingTarget", () => {
  it("TC-FE-190: stands aside for anything holding text", () => {
    expect(isTypingTarget(element("<textarea></textarea>"))).toBe(true);
    expect(isTypingTarget(element('<input type="text" />'))).toBe(true);
    expect(isTypingTarget(element("<input />"))).toBe(true);
    expect(isTypingTarget(element('<input type="search" />'))).toBe(true);
    expect(isTypingTarget(contentEditable())).toBe(true);
    expect(isTypingTarget(element("<select></select>"))).toBe(true);
  });

  it("TC-FE-191: does not stand aside for a checkbox, where arrow keys do nothing", () => {
    // The bug this pins: ticking the debug toggle used to disable the arrow keys entirely,
    // because every `input` counted as somewhere text was being entered.
    expect(isTypingTarget(element('<input type="checkbox" />'))).toBe(false);
    expect(isTypingTarget(element('<input type="radio" />'))).toBe(false);
    expect(isTypingTarget(element('<input type="file" />'))).toBe(false);
    expect(isTypingTarget(element('<input type="submit" />'))).toBe(false);
  });

  it("TC-FE-192: ordinary elements and non-elements are not typing targets", () => {
    expect(isTypingTarget(element("<p>text</p>"))).toBe(false);
    expect(isTypingTarget(element("<button></button>"))).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
    expect(isTypingTarget(new EventTarget())).toBe(false);
  });
});

describe("activatesOnSpace", () => {
  it("TC-FE-193: stands aside for anything the space bar operates", () => {
    expect(activatesOnSpace(element("<button></button>"))).toBe(true);
    expect(activatesOnSpace(element('<input type="checkbox" />'))).toBe(true);
    expect(activatesOnSpace(element('<input type="radio" />'))).toBe(true);
    expect(activatesOnSpace(element('<div role="button"></div>'))).toBe(true);
    expect(activatesOnSpace(element("<summary></summary>"))).toBe(true);
    expect(activatesOnSpace(element("<select></select>"))).toBe(true);
  });

  it("TC-FE-194: a text field is not space-activated; it is just typing", () => {
    expect(activatesOnSpace(element('<input type="text" />'))).toBe(false);
    expect(activatesOnSpace(element("<textarea></textarea>"))).toBe(false);
    expect(activatesOnSpace(element("<p></p>"))).toBe(false);
    expect(activatesOnSpace(null)).toBe(false);
  });
});
