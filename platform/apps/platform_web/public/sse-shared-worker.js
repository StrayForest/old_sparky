const streams = new Map();

function broadcast(entry, message) {
  for (const port of entry.ports) {
    try {
      port.postMessage({ key: entry.key, ...message });
    } catch {
      entry.ports.delete(port);
    }
  }
}

function stopEntry(entry) {
  if (entry.source) {
    entry.source.close();
    entry.source = null;
  }
  if (streams.get(entry.key) === entry) {
    streams.delete(entry.key);
  }
}

function startEntry(entry, url) {
  try {
    entry.source = new EventSource(url, { withCredentials: true });
    entry.source.onopen = () => broadcast(entry, { type: "open" });
    entry.source.addEventListener("bracket", (event) => {
      broadcast(entry, { type: "bracket", data: event.data });
    });
    entry.source.onerror = () => {
      broadcast(entry, { type: "error" });
      stopEntry(entry);
    };
  } catch {
    broadcast(entry, { type: "error" });
    stopEntry(entry);
  }
  return entry;
}

self.onconnect = (event) => {
  const port = event.ports[0];
  port.start();
  port.onmessage = ({ data }) => {
    if (!data || typeof data.key !== "string") {
      return;
    }
    if (data.type === "subscribe" && typeof data.url === "string") {
      let entry = streams.get(data.key);
      if (!entry) {
        entry = { key: data.key, ports: new Set([port]), source: null };
        streams.set(data.key, entry);
        startEntry(entry, data.url);
      }
      entry.ports.add(port);
      if (entry.source?.readyState === EventSource.OPEN) {
        port.postMessage({ key: entry.key, type: "open" });
      }
      return;
    }
    if (data.type === "unsubscribe") {
      const entry = streams.get(data.key);
      if (!entry) {
        return;
      }
      entry.ports.delete(port);
      if (entry.ports.size === 0) {
        stopEntry(entry);
      }
    }
  };
};
