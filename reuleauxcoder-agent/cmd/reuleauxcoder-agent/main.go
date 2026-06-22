package main

import (
	"context"
	"flag"
	"log"
	"os"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/runner"
)

func main() {
	var (
		host            string
		bootstrapToken  string
		cwd             string
		workspaceRoot   string
		peerInfoFile    string
		claimInterval   time.Duration
		interactive     bool
		agentRun        bool
		workerSessionID string
		workerKind      string
	)

	flag.StringVar(&host, "host", "", "Labrastro server base URL")
	flag.StringVar(&bootstrapToken, "bootstrap-token", "", "One-time bootstrap token")
	flag.StringVar(&cwd, "cwd", "", "Working directory for local action execution")
	flag.StringVar(&workspaceRoot, "workspace-root", "", "Workspace root reported to host")
	flag.StringVar(&peerInfoFile, "peer-info-file", "", "Write peer registration info to this JSON file")
	flag.DurationVar(&claimInterval, "claim-interval", 500*time.Millisecond, "Interval between claim attempts when no work is available")
	flag.BoolVar(&interactive, "interactive", false, "Run interactive chat loop proxied through host")
	flag.BoolVar(&agentRun, "agent-run-worker", false, "Run AgentRun worker loop")
	flag.StringVar(&workerSessionID, "worker-session-id", "", "Stable worker session id")
	flag.StringVar(&workerKind, "agent-run-worker-kind", "local_peer", "AgentRun worker kind: local_peer, server_worker, or sandbox_worker")
	flag.Parse()

	if host == "" {
		log.Print("missing required --host")
		os.Exit(2)
	}
	if bootstrapToken == "" {
		log.Print("missing required --bootstrap-token")
		os.Exit(2)
	}

	r := runner.New(runner.Config{
		Host:            host,
		BootstrapToken:  bootstrapToken,
		CWD:             cwd,
		WorkspaceRoot:   workspaceRoot,
		PeerInfoFile:    peerInfoFile,
		ClaimInterval:   claimInterval,
		Interactive:     interactive,
		AgentRun:        agentRun,
		WorkerSessionID: workerSessionID,
		WorkerKind:      workerKind,
	})
	if err := r.Run(context.Background()); err != nil {
		log.Printf("agent exited with error: %v", err)
		os.Exit(1)
	}
}
