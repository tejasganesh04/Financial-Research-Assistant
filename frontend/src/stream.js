// SSE messages can span network chunks. Decode bytes first, then complete lines.
// TRANSPORT: Network chunks are not complete events. Buffer bytes/lines until one SSE message is complete.
export async function readEvents(stream, onEvent) {
  if (!stream?.getReader)
    throw new Error("This browser could not open the response stream.");
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data = [];
  let terminal = false;
  // Decode one complete JSON event and pass it to App.jsx. Final/pending/error marks a terminal response.
  const dispatch = () => {
    if (!data.length) return;
    const event = JSON.parse(data.join("\n"));
    data = [];
    if (["final", "pending_approval", "error"].includes(event.type))
      terminal = true;
    onEvent(event);
  };
  const line = (value) => {
    if (value === "") dispatch();
    else if (value.startsWith("data:"))
      data.push(value.slice(5).replace(/^ /, ""));
    // SSE comments/heartbeats and unknown fields are deliberately ignored.
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done
        ? decoder.decode()
        : decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n")) !== -1) {
        line(buffer.slice(0, end).replace(/\r$/, ""));
        buffer = buffer.slice(end + 1);
      }
      if (done) break;
    }
    if (buffer) line(buffer.replace(/\r$/, ""));
    dispatch();
    // A closed connection without a terminal event is incomplete, so ask the user to check history instead of resubmitting.
    if (!terminal)
      throw new Error(
        "The connection ended before the answer finished. Check saved history before trying again.",
      );
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}
