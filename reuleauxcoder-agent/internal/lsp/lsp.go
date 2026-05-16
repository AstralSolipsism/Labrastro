package lsp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

type language string

const (
	python     language = "python"
	rust       language = "rust"
	goLang     language = "go"
	typescript language = "typescript"
	javascript language = "javascript"
	cLang      language = "c"
	cpp        language = "cpp"
	bash       language = "bash"
	yaml       language = "yaml"
)

type serverCommand struct {
	Command string
	Args    []string
}

var extToLanguage = map[string]language{
	".py": python, ".pyi": python,
	".rs": rust,
	".go": goLang,
	".ts": typescript, ".tsx": typescript,
	".js": javascript, ".jsx": javascript, ".mjs": javascript, ".cjs": javascript,
	".c": cLang, ".h": cLang,
	".cpp": cpp, ".cc": cpp, ".cxx": cpp, ".hpp": cpp, ".hxx": cpp, ".hh": cpp, ".ino": cpp, ".pde": cpp,
	".sh": bash, ".bash": bash,
	".yaml": yaml, ".yml": yaml,
}

var languageIDs = map[language]string{
	python: "python", rust: "rust", goLang: "go", typescript: "typescript", javascript: "javascript",
	cLang: "c", cpp: "cpp", bash: "shellscript", yaml: "yaml",
}

var serverCommands = map[language]serverCommand{
	python: {
		Command: "npx",
		Args:    []string{"-y", "--package", "pyright", "pyright-langserver", "--stdio"},
	},
	rust:   {Command: "rust-analyzer"},
	goLang: {Command: "gopls", Args: []string{"serve"}},
	typescript: {
		Command: "npx",
		Args: []string{
			"-y",
			"--package", "typescript",
			"--package", "typescript-language-server",
			"typescript-language-server",
			"--stdio",
		},
	},
	javascript: {
		Command: "npx",
		Args: []string{
			"-y",
			"--package", "typescript",
			"--package", "typescript-language-server",
			"typescript-language-server",
			"--stdio",
		},
	},
	cLang: {Command: "clangd"},
	cpp:   {Command: "clangd"},
	bash:  {Command: "npx", Args: []string{"-y", "bash-language-server", "start"}},
	yaml:  {Command: "npx", Args: []string{"-y", "yaml-language-server", "--stdio"}},
}

var rootMarkers = map[language][]string{
	python:     {"pyproject.toml", "setup.py", "setup.cfg"},
	rust:       {"Cargo.toml"},
	goLang:     {"go.mod"},
	typescript: {"tsconfig.json", "package.json"},
	javascript: {"package.json"},
	cLang:      {"compile_commands.json", "Makefile", "CMakeLists.txt"},
	cpp:        {"compile_commands.json", "Makefile", "CMakeLists.txt"},
}

type rpcMessage struct {
	JSONRPC string         `json:"jsonrpc,omitempty"`
	ID      any            `json:"id,omitempty"`
	Method  string         `json:"method,omitempty"`
	Params  map[string]any `json:"params,omitempty"`
	Result  any            `json:"result,omitempty"`
	Error   any            `json:"error,omitempty"`
}

type client struct {
	command      serverCommand
	root         string
	cmd          *exec.Cmd
	stdin        io.WriteCloser
	stdout       io.ReadCloser
	nextID       int
	mu           sync.Mutex
	pending      map[int]chan rpcMessage
	diagnostics  map[string][]map[string]any
	diagnosticCh map[string]chan struct{}
	done         chan struct{}
	shutdownOnce sync.Once
}

type Manager struct {
	mu      sync.Mutex
	clients map[string]*client
}

var defaultManager = &Manager{clients: map[string]*client{}}

func Available() bool {
	for _, command := range serverCommands {
		if _, err := exec.LookPath(command.Command); err == nil {
			return true
		}
	}
	return false
}

func ShutdownDefault() {
	defaultManager.Shutdown()
}

func ExecuteOperation(args map[string]any, cwd string) (string, error) {
	return defaultManager.ExecuteOperation(args, cwd)
}

func DiagnosticsAfterEdit(filePath string, cwd string) string {
	text, err := defaultManager.Diagnostics(filePath, cwd)
	if err != nil {
		return ""
	}
	return text
}

func (m *Manager) ExecuteOperation(args map[string]any, cwd string) (string, error) {
	operation, _ := args["operation"].(string)
	filePath, _ := args["filePath"].(string)
	if filePath == "" {
		filePath, _ = args["file_path"].(string)
	}
	if strings.TrimSpace(operation) == "" {
		return "", fmt.Errorf("operation must be a non-empty string")
	}
	if strings.TrimSpace(filePath) == "" {
		return "", fmt.Errorf("filePath must be a non-empty string")
	}
	resolved, lang, err := resolveFile(filePath, cwd)
	if err != nil {
		return "", err
	}
	c, err := m.clientFor(resolved, lang, cwd)
	if err != nil {
		return "", err
	}
	if err := c.didOpen(resolved, languageIDs[lang]); err != nil {
		return "", err
	}

	switch operation {
	case "documentSymbol":
		result, err := c.request("textDocument/documentSymbol", map[string]any{
			"textDocument": map[string]any{"uri": pathToURI(resolved)},
		}, 5*time.Second)
		if err != nil {
			return "", err
		}
		return formatDocumentSymbols(result), nil
	case "goToDefinition", "findReferences":
		line, ok := asPositiveInt(args["line"])
		if !ok {
			return "", fmt.Errorf("%s requires positive 1-based line", operation)
		}
		character, ok := asPositiveInt(args["character"])
		if !ok {
			return "", fmt.Errorf("%s requires positive 1-based character", operation)
		}
		method := "textDocument/definition"
		params := map[string]any{
			"textDocument": map[string]any{"uri": pathToURI(resolved)},
			"position": map[string]any{
				"line":      line - 1,
				"character": character - 1,
			},
		}
		if operation == "findReferences" {
			method = "textDocument/references"
			params["context"] = map[string]any{"includeDeclaration": true}
		}
		result, err := c.request(method, params, 5*time.Second)
		if err != nil {
			return "", err
		}
		return formatLocations(result, cwd), nil
	default:
		return "", fmt.Errorf("unsupported LSP operation %q", operation)
	}
}

func (m *Manager) Diagnostics(filePath string, cwd string) (string, error) {
	resolved, lang, err := resolveFile(filePath, cwd)
	if err != nil {
		return "", err
	}
	c, err := m.clientFor(resolved, lang, cwd)
	if err != nil {
		return "", err
	}
	if err := c.didOpen(resolved, languageIDs[lang]); err != nil {
		return "", err
	}
	items := c.waitDiagnostics(pathToURI(resolved), 5*time.Second)
	return renderDiagnostics(relPath(resolved, cwd), items), nil
}

func (m *Manager) Shutdown() {
	m.mu.Lock()
	clients := make([]*client, 0, len(m.clients))
	for _, c := range m.clients {
		clients = append(clients, c)
	}
	m.clients = map[string]*client{}
	m.mu.Unlock()
	for _, c := range clients {
		c.shutdown()
	}
}

func (m *Manager) clientFor(path string, lang language, cwd string) (*client, error) {
	command := serverCommands[lang]
	if command.Command == "" {
		return nil, fmt.Errorf("no language server configured for %s", lang)
	}
	if _, err := exec.LookPath(command.Command); err != nil {
		return nil, fmt.Errorf("language server command not found on PATH: %s", command.Command)
	}
	root := resolveWorkspaceRoot(path, lang, cwd)
	key := string(lang) + "|" + root
	m.mu.Lock()
	c := m.clients[key]
	if c == nil {
		c = &client{
			command:      command,
			root:         root,
			pending:      map[int]chan rpcMessage{},
			diagnostics:  map[string][]map[string]any{},
			diagnosticCh: map[string]chan struct{}{},
			done:         make(chan struct{}),
		}
		m.clients[key] = c
	}
	m.mu.Unlock()
	if err := c.start(); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *client) start() error {
	c.mu.Lock()
	if c.cmd != nil {
		c.mu.Unlock()
		return nil
	}
	cmd := exec.Command(c.command.Command, c.command.Args...)
	cmd.Dir = c.root
	cmd.Stderr = io.Discard
	stdin, err := cmd.StdinPipe()
	if err != nil {
		c.mu.Unlock()
		return err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		c.mu.Unlock()
		return err
	}
	if err := cmd.Start(); err != nil {
		c.mu.Unlock()
		return err
	}
	c.cmd = cmd
	c.stdin = stdin
	c.stdout = stdout
	c.mu.Unlock()

	go c.readLoop()
	_, err = c.request("initialize", map[string]any{
		"processId":    os.Getpid(),
		"rootUri":      pathToURI(c.root),
		"capabilities": map[string]any{},
	}, 10*time.Second)
	if err != nil {
		c.shutdown()
		return err
	}
	return c.notify("initialized", map[string]any{})
}

func (c *client) request(method string, params map[string]any, timeout time.Duration) (any, error) {
	c.mu.Lock()
	c.nextID++
	id := c.nextID
	ch := make(chan rpcMessage, 1)
	c.pending[id] = ch
	c.mu.Unlock()
	if err := c.send(rpcMessage{JSONRPC: "2.0", ID: id, Method: method, Params: params}); err != nil {
		return nil, err
	}
	select {
	case msg := <-ch:
		if msg.Error != nil {
			return nil, fmt.Errorf("%v", msg.Error)
		}
		return msg.Result, nil
	case <-time.After(timeout):
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
		return nil, fmt.Errorf("LSP request timed out: %s", method)
	}
}

func (c *client) notify(method string, params map[string]any) error {
	return c.send(rpcMessage{JSONRPC: "2.0", Method: method, Params: params})
}

func (c *client) didOpen(path string, languageID string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return c.notify("textDocument/didOpen", map[string]any{
		"textDocument": map[string]any{
			"uri":        pathToURI(path),
			"languageId": languageID,
			"version":    1,
			"text":       string(data),
		},
	})
}

func (c *client) waitDiagnostics(uri string, timeout time.Duration) []map[string]any {
	c.mu.Lock()
	ch := c.diagnosticCh[uri]
	if ch == nil {
		ch = make(chan struct{})
		c.diagnosticCh[uri] = ch
	}
	c.mu.Unlock()
	select {
	case <-ch:
	case <-time.After(timeout):
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]map[string]any{}, c.diagnostics[uri]...)
}

func (c *client) send(msg rpcMessage) error {
	body, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stdin == nil {
		return fmt.Errorf("LSP server is not running")
	}
	_, err = fmt.Fprintf(c.stdin, "Content-Length: %d\r\n\r\n%s", len(body), body)
	return err
}

func (c *client) readLoop() {
	reader := bufio.NewReader(c.stdout)
	for {
		length, err := readContentLength(reader)
		if err != nil {
			return
		}
		body := make([]byte, length)
		if _, err := io.ReadFull(reader, body); err != nil {
			return
		}
		var msg rpcMessage
		if err := json.Unmarshal(body, &msg); err != nil {
			continue
		}
		c.handleMessage(msg)
	}
}

func (c *client) handleMessage(msg rpcMessage) {
	if msg.ID != nil {
		id := numericID(msg.ID)
		c.mu.Lock()
		ch := c.pending[id]
		delete(c.pending, id)
		c.mu.Unlock()
		if ch != nil {
			ch <- msg
		}
		return
	}
	if msg.Method != "textDocument/publishDiagnostics" || msg.Params == nil {
		return
	}
	uri, _ := msg.Params["uri"].(string)
	rawItems, _ := msg.Params["diagnostics"].([]any)
	items := make([]map[string]any, 0, len(rawItems))
	for _, raw := range rawItems {
		if item, ok := raw.(map[string]any); ok {
			items = append(items, item)
		}
	}
	c.mu.Lock()
	c.diagnostics[uri] = items
	ch := c.diagnosticCh[uri]
	if ch != nil {
		close(ch)
		c.diagnosticCh[uri] = make(chan struct{})
	}
	c.mu.Unlock()
}

func (c *client) shutdown() {
	c.shutdownOnce.Do(func() {
		if c.cmd == nil {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		done := make(chan struct{})
		go func() {
			_, _ = c.request("shutdown", map[string]any{}, time.Second)
			_ = c.notify("exit", map[string]any{})
			close(done)
		}()
		select {
		case <-done:
		case <-ctx.Done():
		}
		_ = c.cmd.Process.Kill()
		_, _ = c.cmd.Process.Wait()
	})
}

func readContentLength(reader *bufio.Reader) (int, error) {
	length := 0
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return 0, err
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		key, value, ok := strings.Cut(line, ":")
		if ok && strings.EqualFold(strings.TrimSpace(key), "Content-Length") {
			fmt.Sscanf(strings.TrimSpace(value), "%d", &length)
		}
	}
	if length <= 0 {
		return 0, fmt.Errorf("missing Content-Length")
	}
	return length, nil
}

func resolveFile(path string, cwd string) (string, language, error) {
	resolved := path
	if !filepath.IsAbs(resolved) {
		resolved = filepath.Join(cwd, resolved)
	}
	abs, err := filepath.Abs(resolved)
	if err != nil {
		return "", "", err
	}
	lang, ok := extToLanguage[strings.ToLower(filepath.Ext(abs))]
	if !ok {
		return "", "", fmt.Errorf("unsupported file type for LSP: %s", filepath.Ext(abs))
	}
	return abs, lang, nil
}

func resolveWorkspaceRoot(path string, lang language, cwd string) string {
	markers := rootMarkers[lang]
	dir := filepath.Dir(path)
	for {
		for _, marker := range markers {
			if _, err := os.Stat(filepath.Join(dir, marker)); err == nil {
				return dir
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	if cwd != "" {
		if abs, err := filepath.Abs(cwd); err == nil {
			return abs
		}
	}
	return filepath.Dir(path)
}

func renderDiagnostics(path string, items []map[string]any) string {
	type diag struct {
		line, character int
		severity        int
		message         string
	}
	diagnostics := []diag{}
	for _, item := range items {
		severity := numericID(item["severity"])
		if severity == 0 {
			severity = 1
		}
		if severity != 1 {
			continue
		}
		start := nestedMap(nestedMap(item, "range"), "start")
		diagnostics = append(diagnostics, diag{
			line:      numericID(start["line"]) + 1,
			character: numericID(start["character"]) + 1,
			severity:  severity,
			message:   firstLine(fmt.Sprint(item["message"])),
		})
	}
	if len(diagnostics) == 0 {
		return ""
	}
	sort.Slice(diagnostics, func(i, j int) bool {
		if diagnostics[i].line != diagnostics[j].line {
			return diagnostics[i].line < diagnostics[j].line
		}
		return diagnostics[i].character < diagnostics[j].character
	})
	var b strings.Builder
	fmt.Fprintf(&b, "<diagnostics file=\"%s\">\n", filepath.ToSlash(path))
	for i, d := range diagnostics {
		if i >= 20 {
			break
		}
		fmt.Fprintf(&b, "  ERROR [%d:%d] %s\n", d.line, d.character, d.message)
	}
	b.WriteString("</diagnostics>")
	return b.String()
}

func formatLocations(result any, cwd string) string {
	locations := []any{}
	if list, ok := result.([]any); ok {
		locations = list
	} else if result != nil {
		locations = append(locations, result)
	}
	if len(locations) == 0 {
		return "No LSP results."
	}
	lines := []string{}
	for _, raw := range locations {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		uri, _ := item["uri"].(string)
		if uri == "" {
			uri, _ = item["targetUri"].(string)
		}
		rangeMap := nestedMap(item, "range")
		if len(rangeMap) == 0 {
			rangeMap = nestedMap(item, "targetSelectionRange")
		}
		start := nestedMap(rangeMap, "start")
		lines = append(lines, fmt.Sprintf("%s:%d:%d", uriDisplayPath(uri, cwd), numericID(start["line"])+1, numericID(start["character"])+1))
	}
	if len(lines) == 0 {
		return "No LSP results."
	}
	return strings.Join(lines, "\n")
}

func formatDocumentSymbols(result any) string {
	items, ok := result.([]any)
	if !ok || len(items) == 0 {
		return "No document symbols."
	}
	lines := flattenSymbols(items, "")
	if len(lines) == 0 {
		return "No document symbols."
	}
	return strings.Join(lines, "\n")
}

func flattenSymbols(items []any, prefix string) []string {
	lines := []string{}
	for _, raw := range items {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		name := fmt.Sprint(item["name"])
		selection := nestedMap(item, "selectionRange")
		if len(selection) == 0 {
			selection = nestedMap(item, "range")
		}
		start := nestedMap(selection, "start")
		lines = append(lines, fmt.Sprintf("%s%s:%d:%d", prefix, name, numericID(start["line"])+1, numericID(start["character"])+1))
		if children, ok := item["children"].([]any); ok {
			lines = append(lines, flattenSymbols(children, prefix+name+".")...)
		}
	}
	return lines
}

func pathToURI(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		abs = path
	}
	u := url.URL{Scheme: "file", Path: filepath.ToSlash(abs)}
	if runtime.GOOS == "windows" && len(u.Path) >= 2 && u.Path[1] == ':' {
		u.Path = "/" + u.Path
	}
	return u.String()
}

func uriDisplayPath(uri string, cwd string) string {
	parsed, err := url.Parse(uri)
	if err != nil || parsed.Scheme != "file" {
		return uri
	}
	path := parsed.Path
	if runtime.GOOS == "windows" && len(path) >= 3 && path[0] == '/' && path[2] == ':' {
		path = path[1:]
	}
	path, _ = url.PathUnescape(path)
	path = filepath.FromSlash(path)
	return relPath(path, cwd)
}

func relPath(path string, cwd string) string {
	if cwd != "" {
		if rel, err := filepath.Rel(cwd, path); err == nil && !strings.HasPrefix(rel, "..") {
			return filepath.ToSlash(rel)
		}
	}
	return filepath.ToSlash(path)
}

func nestedMap(item map[string]any, key string) map[string]any {
	if item == nil {
		return map[string]any{}
	}
	value, _ := item[key].(map[string]any)
	if value == nil {
		return map[string]any{}
	}
	return value
}

func numericID(value any) int {
	switch n := value.(type) {
	case int:
		return n
	case int32:
		return int(n)
	case int64:
		return int(n)
	case float64:
		return int(n)
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	default:
		return 0
	}
}

func asPositiveInt(value any) (int, bool) {
	n := numericID(value)
	return n, n > 0
}

func firstLine(value string) string {
	value = strings.ReplaceAll(value, "\r\n", "\n")
	if idx := strings.IndexByte(value, '\n'); idx >= 0 {
		return value[:idx]
	}
	return value
}

func DecodeTestMessage(body []byte) (map[string]any, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	out := map[string]any{}
	err := decoder.Decode(&out)
	return out, err
}
