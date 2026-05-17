import express, { type Request, Response, NextFunction } from "express";
import { registerRoutes } from "./routes";
import { spawn } from "child_process";
import path from "path";
import fs from "fs";

// Standalone utilities (no vite dependency)
function log(message: string, source = "express") {
  const formattedTime = new Date().toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
  console.log(`${formattedTime} [${source}] ${message}`);
}

function serveStaticProduction(app: any) {
  const distPath = path.resolve(import.meta.dirname, "public");
  
  if (!fs.existsSync(distPath)) {
    // In development, vite handles serving. In production, client must be built first.
    if (process.env.NODE_ENV === "production") {
      throw new Error(
        `Could not find the build directory: ${distPath}, make sure to build the client first`,
      );
    }
    // In development, if dist doesn't exist, that's ok - vite will handle it
    return;
  }

  app.use(express.static(distPath));
  
  // fall through to index.html if the file doesn't exist
  app.use("*", (_req: any, res: any) => {
    res.sendFile(path.resolve(distPath, "index.html"));
  });
}

const app = express();

declare module 'http' {
  interface IncomingMessage {
    rawBody: unknown
  }
}
app.use(express.json({
  verify: (req, _res, buf) => {
    req.rawBody = buf;
  }
}));
app.use(express.urlencoded({ extended: false }));

app.use((req, res, next) => {
  const start = Date.now();
  const path = req.path;
  let capturedJsonResponse: Record<string, any> | undefined = undefined;

  const originalResJson = res.json;
  res.json = function (bodyJson, ...args) {
    capturedJsonResponse = bodyJson;
    return originalResJson.apply(res, [bodyJson, ...args]);
  };

  res.on("finish", () => {
    const duration = Date.now() - start;
    if (path.startsWith("/api")) {
      let logLine = `${req.method} ${path} ${res.statusCode} in ${duration}ms`;
      if (capturedJsonResponse) {
        logLine += ` :: ${JSON.stringify(capturedJsonResponse)}`;
      }

      if (logLine.length > 80) {
        logLine = logLine.slice(0, 79) + "…";
      }

      log(logLine);
    }
  });

  next();
});

// Start Python microservice
function startPythonService() {
  const pythonPath = path.join(process.cwd(), "po_service.py");
  const pythonProcess = spawn("python3", [pythonPath], {
    stdio: ["inherit", "pipe", "pipe"],
    detached: false,
  });

  pythonProcess.stdout?.on("data", (data) => {
    console.log(`[PO_SERVICE] ${data.toString().trim()}`);
  });

  pythonProcess.stderr?.on("data", (data) => {
    console.error(`[PO_SERVICE_ERR] ${data.toString().trim()}`);
  });

  pythonProcess.on("error", (err) => {
    console.error("[PO_SERVICE] Failed to start:", err);
  });

  process.on("exit", () => {
    pythonProcess.kill();
  });

  return pythonProcess;
}

(async () => {
  // Start Python service first
  log("Starting Python microservice...");
  startPythonService();

  // Wait a bit for Python service to be ready
  await new Promise((resolve) => setTimeout(resolve, 2000));

  const server = await registerRoutes(app);

  app.use((err: any, _req: Request, res: Response, _next: NextFunction) => {
    const status = err.status || err.statusCode || 500;
    const message = err.message || "Internal Server Error";

    res.status(status).json({ message });
    throw err;
  });

  // Serve frontend
  const distPath = path.resolve(import.meta.dirname, "public");
  
  // Production mode: always serve static files (vite NOT bundled)
  if (process.env.NODE_ENV === "production") {
    log("Production mode - serving static files");
    serveStaticProduction(app);
  }
  // Development mode with dist: serve static files
  else if (fs.existsSync(distPath)) {
    log("Development mode - serving static files (dist exists)");
    serveStaticProduction(app);
  }
  // Development mode without dist: try Vite HMR
  else {
    try {
      const viteModule = await import("./vite.js");
      await viteModule.setupVite(app, server);
      log("Vite development server ready");
    } catch (err) {
      log(`Vite unavailable: ${err instanceof Error ? err.message : "unknown error"}`);
      serveStaticProduction(app);
    }
  }

  // ALWAYS serve the app on the port specified in the environment variable PORT
  // Other ports are firewalled. Default to 5000 if not specified.
  // this serves both the API and the client.
  // It is the only port that is not firewalled.
  const port = parseInt(process.env.PORT || '5000', 10);
  server.listen({
    port,
    host: "0.0.0.0",
    reusePort: true,
  }, () => {
    log(`serving on port ${port}`);
  });
})();
