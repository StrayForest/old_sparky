import { z } from "zod";

// Must run before any client schema is constructed. Besides disabling object
// parser code generation, this prevents Zod's caught `new Function` CSP probe.
z.config({ jitless: true });

export { z };
