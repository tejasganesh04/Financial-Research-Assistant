import assert from "node:assert/strict";
import test from "node:test";
import { readEvents } from "../src/stream.js";

function stream(parts) {
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    },
  });
}

test("decodes split UTF-8, CRLF, comments and multiline data", async () => {
  const encoder = new TextEncoder();
  const bytes = encoder.encode(
    ': heartbeat\r\ndata: {"type":"node_start",\r\ndata: "label":"€ revenue"}\r\n\r\ndata: {"type":"final"}\r\n\r\n',
  );
  const parts = Array.from(bytes, (byte) => new Uint8Array([byte]));
  const events = [];
  await readEvents(stream(parts), (e) => events.push(e));
  assert.equal(events[0].label, "€ revenue");
  assert.equal(events[1].type, "final");
});

test("an interrupted response is not treated as a completed answer", async () => {
  await assert.rejects(
    readEvents(
      stream([
        new TextEncoder().encode(
          'data: {"type":"node_start","label":"Searching documents"}\n\n',
        ),
      ]),
      () => {},
    ),
    /before the answer finished/,
  );
});

test("a pending approval is terminal even without a final newline", async () => {
  const events = [];
  await readEvents(
    stream([new TextEncoder().encode('data: {"type":"pending_approval"}')]),
    (e) => events.push(e),
  );
  assert.equal(events[0].type, "pending_approval");
});

test("malformed JSON and server errors propagate to the caller", async () => {
  await assert.rejects(
    readEvents(
      stream([new TextEncoder().encode("data: broken\n\n")]),
      () => {},
    ),
    SyntaxError,
  );
  await assert.rejects(
    readEvents(
      stream([
        new TextEncoder().encode(
          'data: {"type":"error","message":"Unavailable"}\n\n',
        ),
      ]),
      (e) => {
        throw new Error(e.message);
      },
    ),
    /Unavailable/,
  );
});
