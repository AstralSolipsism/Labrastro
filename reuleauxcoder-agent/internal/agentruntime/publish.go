package agentruntime

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

type PublishOptions struct {
	EventSink func(Event)
}

type PublishResult struct {
	Artifacts []map[string]any
	Events    []Event
}

func PublishWorktree(ctx context.Context, req RunRequest, opts PublishOptions) PublishResult {
	publisher := worktreePublisher{ctx: ctx, req: req, opts: opts}
	return publisher.run()
}

type worktreePublisher struct {
	ctx    context.Context
	req    RunRequest
	opts   PublishOptions
	result PublishResult
}

func (p *worktreePublisher) run() PublishResult {
	role := worktreeRole(p.req)
	if role != "target" {
		p.emit(Event{
			Type: EventStatus,
			Data: map[string]any{
				"status":        "publish_skipped",
				"reason":        "worktree_role_not_target",
				"worktree_role": role,
			},
		})
		return p.result
	}
	policy := publishPolicy(p.req)
	if policy == "never" {
		p.emit(Event{
			Type: EventStatus,
			Data: map[string]any{
				"status": "publish_skipped",
				"reason": "publish_policy_never",
			},
		})
		return p.result
	}
	workdir := strings.TrimSpace(p.req.Workdir)
	branch := strings.TrimSpace(p.req.Branch)
	if workdir == "" {
		p.fail("push", "workdir is required for worktree publish")
		return p.result
	}
	if branch == "" {
		current, err := gitOutput(p.ctx, workdir, "rev-parse", "--abbrev-ref", "HEAD")
		if err != nil {
			p.fail("push", err.Error())
			return p.result
		}
		branch = strings.TrimSpace(current)
	}

	p.emit(Event{
		Type: EventStatus,
		Data: map[string]any{
			"status":  "publishing_branch",
			"workdir": workdir,
			"branch":  branch,
		},
	})
	status, err := gitOutput(p.ctx, workdir, "status", "--porcelain", "--untracked-files=all")
	if err != nil {
		p.fail("push", err.Error())
		return p.result
	}
	if strings.TrimSpace(status) == "" {
		p.emit(Event{
			Type: EventStatus,
			Data: map[string]any{
				"status":     "publish_skipped",
				"branch":     branch,
				"no_changes": true,
			},
		})
		p.result.Artifacts = append(p.result.Artifacts, map[string]any{
			"type":        "report",
			"status":      "generated",
			"branch_name": branch,
			"content":     "No worktree changes detected.",
			"metadata": map[string]any{
				"no_changes": true,
				"workdir":    workdir,
			},
		})
		return p.result
	}

	if err := git(p.ctx, workdir, "add", "-A"); err != nil {
		p.fail("push", err.Error())
		return p.result
	}
	authorName := metadataString(p.req.Metadata, "git_author_name")
	if authorName == "" {
		authorName = "Labrastro Agent"
	}
	authorEmail := metadataString(p.req.Metadata, "git_author_email")
	if authorEmail == "" {
		authorEmail = "agent@labrastro.local"
	}
	message := metadataString(p.req.Metadata, "commit_message")
	if message == "" {
		message = "agent: " + p.req.TaskID
	}
	if err := git(
		p.ctx,
		workdir,
		"-c", "user.name="+authorName,
		"-c", "user.email="+authorEmail,
		"commit", "-m", message,
	); err != nil {
		p.fail("push", err.Error())
		return p.result
	}
	if err := git(p.ctx, workdir, "push", "-u", "origin", branch); err != nil {
		p.fail("push", err.Error())
		return p.result
	}

	repoURL := metadataString(p.req.Metadata, "repo_url")
	if repoURL == "" {
		repoURL, _ = gitOutput(p.ctx, workdir, "remote", "get-url", "origin")
		repoURL = strings.TrimSpace(repoURL)
	}
	headSHA, _ := gitOutput(p.ctx, workdir, "rev-parse", "HEAD")
	headSHA = strings.TrimSpace(headSHA)
	baseRef := metadataString(p.req.Metadata, "pr_base")
	if baseRef == "" {
		if resolved, err := remoteDefaultBranchFromWorktree(p.ctx, workdir); err == nil {
			baseRef = resolved
		}
	}
	prTitle := metadataString(p.req.Metadata, "pr_title")
	if prTitle == "" {
		prTitle = "Agent task " + p.req.TaskID
	}
	prBody := metadataString(p.req.Metadata, "pr_body")
	if prBody == "" {
		prBody = defaultPRBody(p.req, workdir, branch)
	}
	p.emit(Event{
		Type: EventStatus,
		Data: map[string]any{
			"status":   "branch_pushed",
			"branch":   branch,
			"repo_url": repoURL,
		},
	})
	p.result.Artifacts = append(p.result.Artifacts, map[string]any{
		"type":        "branch",
		"status":      "pushed",
		"branch_name": branch,
		"path":        workdir,
		"metadata": map[string]any{
			"repo_url":       repoURL,
			"workdir":        workdir,
			"branch":         branch,
			"head_sha":       headSHA,
			"base_ref":       baseRef,
			"commit_message": message,
			"publish_policy": policy,
			"pr_title":       prTitle,
			"pr_body":        prBody,
			"pr_base":        baseRef,
		},
	})

	if policy == "branch" {
		p.emit(Event{
			Type: EventStatus,
			Data: map[string]any{
				"status":         "pr_skipped",
				"branch":         branch,
				"publish_policy": "branch",
			},
		})
		return p.result
	}

	prURL, err := ensurePullRequest(p.ctx, workdir, branch, baseRef, prTitle, prBody)
	if err != nil {
		p.fail("pr", err.Error())
		return p.result
	}
	p.emit(Event{
		Type: EventStatus,
		Data: map[string]any{
			"status": "pr_created",
			"branch": branch,
			"pr_url": prURL,
		},
	})
	p.result.Artifacts = append(p.result.Artifacts, map[string]any{
		"type":    "pull_request",
		"status":  "opened",
		"pr_url":  prURL,
		"content": prBody,
		"metadata": map[string]any{
			"repo_url":       repoURL,
			"workdir":        workdir,
			"branch":         branch,
			"head_sha":       headSHA,
			"base_ref":       baseRef,
			"publish_policy": policy,
			"pr_title":       prTitle,
			"pr_body":        prBody,
			"pr_base":        baseRef,
		},
	})
	return p.result
}

func (p *worktreePublisher) emit(event Event) {
	p.result.Events = append(p.result.Events, event)
	if p.opts.EventSink != nil {
		p.opts.EventSink(event)
	}
}

func (p *worktreePublisher) fail(stage, message string) {
	p.emit(Event{
		Type: EventStatus,
		Data: map[string]any{
			"status": "publish_failed",
			"stage":  stage,
			"error":  message,
		},
	})
	p.result.Artifacts = append(p.result.Artifacts, map[string]any{
		"type":    "log",
		"status":  "failed",
		"content": message,
		"metadata": map[string]any{
			"stage": stage,
		},
	})
}

func defaultPRBody(req RunRequest, workdir, branch string) string {
	lines := []string{
		"Agent runtime task completed.",
		"",
		"Task: " + req.TaskID,
		"Issue: " + req.IssueID,
		"Agent: " + req.AgentID,
		"Branch: " + branch,
		"Workdir: " + workdir,
	}
	return strings.Join(lines, "\n")
}

func worktreeRole(req RunRequest) string {
	role := strings.TrimSpace(strings.ToLower(req.WorktreeRole))
	if role == "" {
		role = strings.TrimSpace(strings.ToLower(metadataString(req.Metadata, "worktree_role")))
	}
	if role == "" {
		return "target"
	}
	return role
}

func publishPolicy(req RunRequest) string {
	policy := strings.TrimSpace(strings.ToLower(req.PublishPolicy))
	if policy == "" {
		policy = strings.TrimSpace(strings.ToLower(metadataString(req.Metadata, "publish_policy")))
	}
	switch policy {
	case "branch", "pr":
		return policy
	default:
		return "never"
	}
}

func ensurePullRequest(ctx context.Context, workdir, branch, baseRef, title, body string) (string, error) {
	viewArgs := []string{"pr", "view", branch, "--json", "url", "--jq", ".url"}
	if existing, err := ghOutput(ctx, workdir, viewArgs...); err == nil {
		if url := strings.TrimSpace(existing); url != "" {
			return url, nil
		}
	}
	args := []string{"pr", "create", "--head", branch, "--title", title, "--body", body}
	if baseRef != "" {
		args = append(args, "--base", baseRef)
	}
	out, err := ghOutput(ctx, workdir, args...)
	if err != nil {
		return "", err
	}
	url := strings.TrimSpace(out)
	if url == "" {
		return "", fmt.Errorf("gh pr create returned empty url")
	}
	return url, nil
}

func ghOutput(ctx context.Context, dir string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "gh", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
	out, err := cmd.CombinedOutput()
	if err != nil {
		text := strings.TrimSpace(string(out))
		if text != "" {
			return string(out), fmt.Errorf("gh %s failed: %w: %s", strings.Join(args, " "), err, text)
		}
		return string(out), fmt.Errorf("gh %s failed: %w", strings.Join(args, " "), err)
	}
	return string(out), nil
}

func remoteDefaultBranchFromWorktree(ctx context.Context, workdir string) (string, error) {
	out, err := gitOutput(ctx, workdir, "symbolic-ref", "refs/remotes/origin/HEAD")
	if err == nil {
		branch := strings.TrimPrefix(strings.TrimSpace(out), "refs/remotes/origin/")
		if branch != "" && branch != "HEAD" {
			return branch, nil
		}
	}
	out, err = gitOutput(ctx, workdir, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(out, "\n") {
		branch := strings.TrimSpace(strings.TrimPrefix(line, "origin/"))
		if branch != "" && branch != "HEAD" {
			return branch, nil
		}
	}
	return "", fmt.Errorf("unable to determine remote default branch for %s", workdir)
}

func metadataString(metadata map[string]any, key string) string {
	if len(metadata) == 0 {
		return ""
	}
	if value, ok := metadata[key]; ok && value != nil {
		return strings.TrimSpace(fmt.Sprint(value))
	}
	return ""
}
