package agentruntime

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPublishWorktreeCommitsPushesAndReportsBranchArtifact(t *testing.T) {
	requireGit(t)
	origin := createGitRepo(t)
	worktree := createPublishWorktree(t, origin, "task-publish-create")
	writeWorktreeFile(t, worktree.Path, "feature.txt", "created by agent\n")

	result := PublishWorktree(context.Background(), RunRequest{
		TaskID:        "task-publish-create",
		AgentID:       "coder",
		PublishPolicy: "branch",
		Workdir:       worktree.Path,
		Branch:        worktree.BranchName,
		Metadata:      map[string]any{"repo_url": origin, "pr_body": "body"},
	}, PublishOptions{})

	branchArtifact := assertArtifact(t, result.Artifacts, "branch", "pushed")
	if artifactOfType(result.Artifacts, "pull_request") != nil {
		t.Fatalf("worker must not create pull_request artifacts: %#v", result.Artifacts)
	}
	metadata, _ := branchArtifact["metadata"].(map[string]any)
	for _, key := range []string{"repo_url", "branch", "head_sha", "base_ref", "commit_message", "publish_policy", "pr_title", "pr_body", "pr_base"} {
		if _, ok := metadata[key]; !ok {
			t.Fatalf("branch metadata missing %s: %#v", key, metadata)
		}
	}
	assertRemoteBranch(t, worktree.Path, worktree.BranchName)
	author := strings.TrimSpace(runGitOutput(t, worktree.Path, "log", "-1", "--format=%an <%ae>"))
	if author != "Labrastro Agent <agent@labrastro.local>" {
		t.Fatalf("commit author = %q", author)
	}
	if !hasPublishStatus(result.Events, "branch_pushed") || hasPublishStatus(result.Events, "pr_created") {
		t.Fatalf("publish events missing: %#v", result.Events)
	}
}

func TestPublishWorktreeReportsNoChanges(t *testing.T) {
	requireGit(t)
	origin := createGitRepo(t)
	worktree := createPublishWorktree(t, origin, "task-publish-clean")

	result := PublishWorktree(context.Background(), RunRequest{
		TaskID:        "task-publish-clean",
		AgentID:       "coder",
		PublishPolicy: "branch",
		Workdir:       worktree.Path,
		Branch:        worktree.BranchName,
		Metadata:      map[string]any{"repo_url": origin, "pr_body": "body"},
	}, PublishOptions{})

	artifact := assertArtifact(t, result.Artifacts, "report", "generated")
	metadata, _ := artifact["metadata"].(map[string]any)
	if metadata["no_changes"] != true {
		t.Fatalf("expected no_changes metadata: %#v", artifact)
	}
	if hasPublishStatus(result.Events, "branch_pushed") {
		t.Fatalf("clean worktree should not push: %#v", result.Events)
	}
}

func TestPublishWorktreeDoesNotCallGH(t *testing.T) {
	requireGit(t)
	origin := createGitRepo(t)
	worktree := createPublishWorktree(t, origin, "task-publish-existing")
	writeWorktreeFile(t, worktree.Path, "existing.txt", "change\n")
	logPath := installFakeGH(t)

	result := PublishWorktree(context.Background(), RunRequest{
		TaskID:        "task-publish-existing",
		AgentID:       "coder",
		PublishPolicy: "branch",
		Workdir:       worktree.Path,
		Branch:        worktree.BranchName,
		Metadata:      map[string]any{"repo_url": origin, "pr_body": "body"},
	}, PublishOptions{})

	assertArtifact(t, result.Artifacts, "branch", "pushed")
	if content, _ := os.ReadFile(logPath); len(content) > 0 {
		t.Fatalf("gh should not be called by PublishWorktree: %s", content)
	}
}

func TestPublishWorktreeBranchPolicyDoesNotCreatePR(t *testing.T) {
	requireGit(t)
	origin := createGitRepo(t)
	worktree := createPublishWorktree(t, origin, "task-publish-no-pr")
	writeWorktreeFile(t, worktree.Path, "branch-only.txt", "change\n")
	logPath := installFakeGH(t)

	result := PublishWorktree(context.Background(), RunRequest{
		TaskID:        "task-publish-no-pr",
		AgentID:       "coder",
		PublishPolicy: "branch",
		Workdir:       worktree.Path,
		Branch:        worktree.BranchName,
		Metadata:      map[string]any{"repo_url": origin},
	}, PublishOptions{})

	assertArtifact(t, result.Artifacts, "branch", "pushed")
	if artifactOfType(result.Artifacts, "pull_request") != nil {
		t.Fatalf("PR artifact should not be created: %#v", result.Artifacts)
	}
	if content, _ := os.ReadFile(logPath); len(content) > 0 {
		t.Fatalf("gh should not be called when publish_policy=branch: %s", content)
	}
}

func TestPublishWorktreeNeverPolicySkipsBeforeGitStatus(t *testing.T) {
	result := PublishWorktree(context.Background(), RunRequest{
		TaskID:        "task-publish-never",
		AgentID:       "coder",
		PublishPolicy: "never",
		Workdir:       filepath.Join(t.TempDir(), "missing-workdir"),
	}, PublishOptions{})

	if len(result.Artifacts) != 0 {
		t.Fatalf("publish_policy=never must not create artifacts: %#v", result.Artifacts)
	}
	if !hasPublishStatus(result.Events, "publish_skipped") || hasPublishStatus(result.Events, "publish_failed") {
		t.Fatalf("publish_policy=never should only skip: %#v", result.Events)
	}
}

func TestPublishWorktreeSourceRoleSkipsEvenWithPublishPolicy(t *testing.T) {
	result := PublishWorktree(context.Background(), RunRequest{
		TaskID:        "task-publish-source",
		AgentID:       "coder",
		WorktreeRole:  "source",
		PublishPolicy: "pr",
		Workdir:       filepath.Join(t.TempDir(), "missing-workdir"),
	}, PublishOptions{})

	if len(result.Artifacts) != 0 {
		t.Fatalf("worktree_role=source must not create artifacts: %#v", result.Artifacts)
	}
	if !hasPublishStatus(result.Events, "publish_skipped") || hasPublishStatus(result.Events, "publish_failed") {
		t.Fatalf("worktree_role=source should only skip: %#v", result.Events)
	}
}

func TestPublishWorktreePRPolicyCreatesPRArtifact(t *testing.T) {
	requireGit(t)
	origin := createGitRepo(t)
	worktree := createPublishWorktree(t, origin, "task-publish-pr")
	writeWorktreeFile(t, worktree.Path, "pr.txt", "change\n")
	logPath := installFakeGH(t)

	result := PublishWorktree(context.Background(), RunRequest{
		TaskID:        "task-publish-pr",
		AgentID:       "coder",
		PublishPolicy: "pr",
		Workdir:       worktree.Path,
		Branch:        worktree.BranchName,
		Metadata: map[string]any{
			"repo_url": origin,
			"pr_title": "Capability update",
			"pr_body":  "body",
		},
	}, PublishOptions{})

	assertArtifact(t, result.Artifacts, "branch", "pushed")
	pr := assertArtifact(t, result.Artifacts, "pull_request", "opened")
	if pr["pr_url"] == "" {
		t.Fatalf("PR artifact missing url: %#v", pr)
	}
	if !hasPublishStatus(result.Events, "pr_created") {
		t.Fatalf("PR policy should emit pr_created: %#v", result.Events)
	}
	content, _ := os.ReadFile(logPath)
	if !strings.Contains(string(content), "pr create") {
		t.Fatalf("gh pr create was not called: %s", content)
	}
}

func TestPublishWorktreePushFailuresBecomeArtifacts(t *testing.T) {
	requireGit(t)
	origin := createGitRepo(t)
	worktree := createPublishWorktree(t, origin, "task-publish-push-fail")
	writeWorktreeFile(t, worktree.Path, "push-fail.txt", "change\n")
	runGit(t, worktree.Path, "remote", "set-url", "origin", localRepoURL(filepath.Join(t.TempDir(), "missing-origin")))

	pushFailed := PublishWorktree(context.Background(), RunRequest{
		TaskID:        "task-publish-push-fail",
		AgentID:       "coder",
		PublishPolicy: "branch",
		Workdir:       worktree.Path,
		Branch:        worktree.BranchName,
		Metadata:      map[string]any{"repo_url": origin, "pr_body": "body"},
	}, PublishOptions{})

	failed := assertArtifact(t, pushFailed.Artifacts, "log", "failed")
	metadata, _ := failed["metadata"].(map[string]any)
	if metadata["stage"] != "push" {
		t.Fatalf("expected push failure metadata: %#v", failed)
	}
}

func TestPublishWorktreeHonorsCancelledContext(t *testing.T) {
	requireGit(t)
	origin := createGitRepo(t)
	worktree := createPublishWorktree(t, origin, "task-publish-cancel")
	writeWorktreeFile(t, worktree.Path, "cancel.txt", "change\n")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	result := PublishWorktree(ctx, RunRequest{
		TaskID:        "task-publish-cancel",
		AgentID:       "coder",
		PublishPolicy: "branch",
		Workdir:       worktree.Path,
		Branch:        worktree.BranchName,
		Metadata:      map[string]any{"repo_url": origin},
	}, PublishOptions{})

	assertArtifact(t, result.Artifacts, "log", "failed")
	if !hasPublishStatus(result.Events, "publish_failed") {
		t.Fatalf("cancelled publish did not emit failure: %#v", result.Events)
	}
}

func createPublishWorktree(t *testing.T, origin, taskID string) *WorktreeResult {
	t.Helper()
	cache, err := NewRepoCache(filepath.Join(t.TempDir(), "cache"))
	if err != nil {
		t.Fatal(err)
	}
	worktree, err := cache.CreateWorktree(context.Background(), WorktreeParams{
		WorkspaceID: "workspace",
		RepoURL:     origin,
		WorkDir:     filepath.Join(t.TempDir(), "work"),
		AgentName:   "coder",
		TaskID:      taskID,
	})
	if err != nil {
		skipIfGitSandboxBlocked(t, err)
		t.Fatal(err)
	}
	return worktree
}

func writeWorktreeFile(t *testing.T, workdir, name, content string) {
	t.Helper()
	path := filepath.Join(workdir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func assertRemoteBranch(t *testing.T, workdir, branch string) {
	t.Helper()
	out := runGitOutput(t, workdir, "ls-remote", "--heads", "origin", branch)
	if !strings.Contains(out, branch) {
		t.Fatalf("remote branch %q missing: %s", branch, out)
	}
}

func assertArtifact(t *testing.T, artifacts []map[string]any, artifactType, status string) map[string]any {
	t.Helper()
	for _, artifact := range artifacts {
		if artifact["type"] == artifactType && artifact["status"] == status {
			return artifact
		}
	}
	t.Fatalf("artifact type=%s status=%s missing: %#v", artifactType, status, artifacts)
	return nil
}

func artifactOfType(artifacts []map[string]any, artifactType string) map[string]any {
	for _, artifact := range artifacts {
		if artifact["type"] == artifactType {
			return artifact
		}
	}
	return nil
}

func hasPublishStatus(events []Event, status string) bool {
	for _, event := range events {
		if event.Type == EventStatus && event.Data["status"] == status {
			return true
		}
	}
	return false
}

func installFakeGH(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	logPath := filepath.Join(dir, "gh.log")
	t.Setenv("LABRASTRO_FAKE_GH_LOG", logPath)
	if os.PathSeparator == '\\' {
		path := filepath.Join(dir, "gh.bat")
		content := "@echo off\r\n" +
			"if \"%1\"==\"pr\" if \"%2\"==\"view\" goto view\r\n" +
			"if \"%1\"==\"pr\" if \"%2\"==\"create\" goto create\r\n" +
			"exit /b 2\r\n" +
			":view\r\n" +
			"echo pr view>>\"%LABRASTRO_FAKE_GH_LOG%\"\r\n" +
			"if not \"%LABRASTRO_FAKE_GH_EXISTING_URL%\"==\"\" echo %LABRASTRO_FAKE_GH_EXISTING_URL%& exit /b 0\r\n" +
			"exit /b 1\r\n" +
			":create\r\n" +
			"echo pr create>>\"%LABRASTRO_FAKE_GH_LOG%\"\r\n" +
			"if \"%LABRASTRO_FAKE_GH_FAIL_CREATE%\"==\"1\" goto fail_create\r\n" +
			"if not \"%LABRASTRO_FAKE_GH_CREATE_URL%\"==\"\" echo %LABRASTRO_FAKE_GH_CREATE_URL%& exit /b 0\r\n" +
			"echo https://example.test/pr/new\r\n" +
			"exit /b 0\r\n" +
			":fail_create\r\n" +
			"echo gh create failed 1>&2\r\n" +
			"exit /b 1\r\n"
		if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
			t.Fatal(err)
		}
	} else {
		path := filepath.Join(dir, "gh")
		content := "#!/bin/sh\n" +
			"if [ \"$1\" = pr ] && [ \"$2\" = view ]; then\n" +
			"  echo pr view >> \"$LABRASTRO_FAKE_GH_LOG\"\n" +
			"  if [ -n \"$LABRASTRO_FAKE_GH_EXISTING_URL\" ]; then echo \"$LABRASTRO_FAKE_GH_EXISTING_URL\"; exit 0; fi\n" +
			"  exit 1\n" +
			"fi\n" +
			"if [ \"$1\" = pr ] && [ \"$2\" = create ]; then\n" +
			"  echo pr create >> \"$LABRASTRO_FAKE_GH_LOG\"\n" +
			"  if [ \"$LABRASTRO_FAKE_GH_FAIL_CREATE\" = 1 ]; then echo gh create failed >&2; exit 1; fi\n" +
			"  if [ -n \"$LABRASTRO_FAKE_GH_CREATE_URL\" ]; then echo \"$LABRASTRO_FAKE_GH_CREATE_URL\"; else echo https://example.test/pr/new; fi\n" +
			"  exit 0\n" +
			"fi\n" +
			"exit 2\n"
		if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return logPath
}
