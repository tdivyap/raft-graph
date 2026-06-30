// raft_graph_extractor is a Layer 1 structural extractor for Go codebases.
// It walks the AST of a Go package and emits a JSON document with:
//   - Entities: STRUCT, INTERFACE, FUNCTION, METHOD, FIELD, TYPE_ALIAS
//   - Relations: HAS_FIELD, HAS_METHOD, EMBEDS, IMPLEMENTS
//
// Output is consumed by a Python Layer 2 that adds semantic interpretation.
package main

import (
	"bytes"         // For exprString's backing buffer
	"encoding/json" // Final JSON marshaling
	"fmt"           // Summary print at the end
	"go/ast"        // AST node types: StructType, FuncDecl, etc.
	"go/printer"    // Renders ast.Expr back to Go source text
	"go/token"      // Position info (line/column) and token constants
	"go/types"      // Resolved type system — used for IMPLEMENTS detection
	"log"           // Fatal errors
	"os"            // File creation
	"strings"       // Trim "*" prefix from receiver types

	"golang.org/x/tools/go/packages" // High-level package loader (modules, deps, type-checking)
)

// ---------- Schema types ----------
// These struct definitions determine the JSON shape.
// The Python Pydantic models will mirror this exactly.

// Entity is a node in the graph: a struct, interface, function, method, field, or type alias.
// JSON tags use `omitempty` for kind-specific fields so the output stays clean —
// a FIELD entity doesn't carry empty `signature` or `num_fields` fields.
type Entity struct {
	// Identification
	ID            string `json:"id"`             // Globally unique: "pkg/path.Name" or "pkg/path.Type.Member"
	Kind          string `json:"kind"`           // STRUCT | INTERFACE | FUNCTION | METHOD | FIELD | TYPE_ALIAS
	Name          string `json:"name"`           // Short name as written ("raft", "Step", "state")
	QualifiedName string `json:"qualified_name"` // Same as ID today; kept separate for future divergence

	// Provenance — where this entity is defined in source
	Package string `json:"package"` // Import path: "go.etcd.io/raft/v3"
	File    string `json:"file"`    // Absolute file path
	Line    int    `json:"line"`    // 1-based line number
	Column  int    `json:"column"`  // 1-based column number

	// Kind-specific fields (omitempty: emitted only when set)
	Signature      string `json:"signature,omitempty"`       // FUNCTION, METHOD: e.g. "func(x int) (string, error)"
	ReceiverType   string `json:"receiver_type,omitempty"`   // METHOD: the type the method is attached to ("raft")
	FieldType      string `json:"field_type,omitempty"`      // FIELD: declared type as Go source ("[]*pb.Entry")
	UnderlyingType string `json:"underlying_type,omitempty"` // TYPE_ALIAS: what it aliases to ("uint64")
	AliasKind      string `json:"alias_kind,omitempty"`      // TYPE_ALIAS: "named_type" or "type_alias"

	// Pointer-to-int so `omitempty` can distinguish "zero count" from "not applicable".
	// A FIELD entity has no NumFields; a STRUCT with zero fields has NumFields=0.
	NumFields  *int `json:"num_fields,omitempty"`  // STRUCT only
	NumMethods *int `json:"num_methods,omitempty"` // INTERFACE only
}

// Relation is an edge in the graph: a directed link from one entity to another.
type Relation struct {
	Kind     string `json:"kind"`                // HAS_FIELD | HAS_METHOD | EMBEDS | IMPLEMENTS
	SourceID string `json:"source_id"`           // ID of the entity where the edge starts
	TargetID string `json:"target_id,omitempty"` // ID of the target entity (empty if target is external)

	// For EMBEDS pointing to external types (sync.Mutex, *log.Logger, *pb.Entry),
	// we don't have entities, so we keep the type as text for Layer 2 to interpret.
	TargetTypeText string `json:"target_type_text,omitempty"`

	// For IMPLEMENTS: most Go methods have pointer receivers, so *MemoryStorage
	// implements Storage (not the value type). This flag records that.
	SourceIsPointer bool `json:"source_is_pointer,omitempty"`

	// Location of the edge in source — where the field/method is declared, etc.
	File string `json:"file,omitempty"`
	Line int    `json:"line,omitempty"`
}

// Graph is the top-level JSON document.
type Graph struct {
	SchemaVersion string     `json:"schema_version"` // "0.1" — bump when schema changes
	Package       string     `json:"package"`        // The package this graph describes
	Entities      []Entity   `json:"entities"`
	Relations     []Relation `json:"relations"`
}

// ---------- Helper ----------

// exprString renders an ast.Expr back to Go-syntax text.
// Example: AST representation of `[]*pb.Entry` becomes the string "[]*pb.Entry".
// Used for field types, function signatures, embedded type names.
func exprString(expr ast.Expr) string {
	var buf bytes.Buffer
	if err := printer.Fprint(&buf, token.NewFileSet(), expr); err != nil {
		return "<unparseable>"
	}
	return buf.String()
}

// calleeID builds the entity ID of a called function/method using the SAME
// scheme as the entity emitter, so CALLS targets line up with real entities:
//   free function : "pkg/path.Name"
//   method        : "pkg/path.RecvType.Name"   (receiver pointer stripped)
// Returns "" for builtins or anything without a resolvable package.
func calleeID(fn *types.Func) string {
	if sig, ok := fn.Type().(*types.Signature); ok && sig.Recv() != nil {
		recv := sig.Recv().Type()
		if ptr, ok := recv.(*types.Pointer); ok {
			recv = ptr.Elem()
		}
		named, ok := recv.(*types.Named)
		if !ok || named.Obj().Pkg() == nil {
			return ""
		}
		return named.Obj().Pkg().Path() + "." + named.Obj().Name() + "." + fn.Name()
	}
	if fn.Pkg() == nil {
		return ""
	}
	return fn.Pkg().Path() + "." + fn.Name()
}

// ---------- Main extraction loop ----------

func main() {
	// packages.Config tells the loader WHAT to load and HOW MUCH info to fetch.
	// Mode flags determine which fields on each returned package are populated.
	cfg := &packages.Config{
		Mode: packages.NeedName | // package name and import path
			packages.NeedFiles | // .go file list
			packages.NeedSyntax | // parsed AST per file
			packages.NeedTypes | // *types.Package — resolved types
			packages.NeedTypesInfo, // use-site → declaration map (needed for IMPLEMENTS)
		Dir: "../../corpus/raft", // Where the corpus lives, relative to tools/ast_walker/
	}

	// "./..." = load this package and everything under it.
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		log.Fatal(err)
	}

	// Accumulate everything before writing — we want one JSON document at the end,
	// not interleaved chunks.
	var entities []Entity
	var relations []Relation
	seenCall := map[string]bool{} // dedup CALLS edges per (source,target)

	for _, pkg := range pkgs {
		// Post 3 scope: focus on the main raft package only.
		// Drop this filter later to include tracker, quorum, raftpb, confchange.
		if pkg.PkgPath != "go.etcd.io/raft/v3" {
			continue
		}

		// === Phase 1: AST-based extraction ===
		// Walk each parsed file's top-level Decls.
		// Two kinds of Decls matter:
		//   - GenDecl: `type X struct {...}`, `type Y interface {...}`, `type Z int`
		//   - FuncDecl: `func F() {...}` or `func (r *raft) M() {...}`
		for _, file := range pkg.Syntax {
			for _, decl := range file.Decls {
				switch d := decl.(type) {

				case *ast.GenDecl:
					// A GenDecl groups one or more specs. `type ( A int; B float )` is one GenDecl
					// with two TypeSpecs. We iterate over the specs.
					for _, spec := range d.Specs {
						// Only TypeSpec interests us. Skip ValueSpec (const/var) and ImportSpec.
						typeSpec, ok := spec.(*ast.TypeSpec)
						if !ok {
							continue
						}
						typePos := pkg.Fset.Position(typeSpec.Pos())
						typeName := typeSpec.Name.Name
						typeID := pkg.PkgPath + "." + typeName

						// The kind of type is encoded in TypeSpec.Type's static type.
						switch t := typeSpec.Type.(type) {

						case *ast.StructType:
							// type X struct { ... }
							// Emit STRUCT entity, then walk fields to emit FIELD entities
							// and HAS_FIELD relations (or EMBEDS relations for embeds).
							numFields := len(t.Fields.List)
							entities = append(entities, Entity{
								ID:            typeID,
								Kind:          "STRUCT",
								Name:          typeName,
								QualifiedName: typeID,
								Package:       pkg.PkgPath,
								File:          typePos.Filename,
								Line:          typePos.Line,
								Column:        typePos.Column,
								NumFields:     &numFields, // pointer so omitempty works
							})

							for _, fieldDef := range t.Fields.List {
								typeStr := exprString(fieldDef.Type)
								if len(fieldDef.Names) == 0 {
									// Embedded field: `MemoryStorage embeds sync.Mutex`.
									// No field name; the type itself acts as the access name.
									embedPos := pkg.Fset.Position(fieldDef.Pos())
									relations = append(relations, Relation{
										Kind:           "EMBEDS",
										SourceID:       typeID,
										TargetTypeText: typeStr, // External type; no ID
										File:           embedPos.Filename,
										Line:           embedPos.Line,
									})
									continue
								}
								// A FieldDecl can declare multiple names: `a, b, c int`.
								// Each name becomes a separate FIELD entity.
								for _, fieldName := range fieldDef.Names {
									fieldPos := pkg.Fset.Position(fieldName.Pos())
									fieldID := typeID + "." + fieldName.Name
									entities = append(entities, Entity{
										ID:            fieldID,
										Kind:          "FIELD",
										Name:          fieldName.Name,
										QualifiedName: fieldID,
										Package:       pkg.PkgPath,
										File:          fieldPos.Filename,
										Line:          fieldPos.Line,
										Column:        fieldPos.Column,
										FieldType:     typeStr,
									})
									relations = append(relations, Relation{
										Kind:     "HAS_FIELD",
										SourceID: typeID,
										TargetID: fieldID,
										File:     fieldPos.Filename,
										Line:     fieldPos.Line,
									})
								}
							}

						case *ast.InterfaceType:
							// type X interface { ... }
							// Same structure as struct: emit INTERFACE, walk methods to emit METHOD
							// entities and HAS_METHOD relations. Embedded interfaces become EMBEDS.
							numMethods := len(t.Methods.List)
							entities = append(entities, Entity{
								ID:            typeID,
								Kind:          "INTERFACE",
								Name:          typeName,
								QualifiedName: typeID,
								Package:       pkg.PkgPath,
								File:          typePos.Filename,
								Line:          typePos.Line,
								Column:        typePos.Column,
								NumMethods:    &numMethods,
							})

							for _, methodField := range t.Methods.List {
								if len(methodField.Names) == 0 {
									// Embedded interface: `io.Reader` inside `interface { io.Reader; Foo() }`.
									typeStr := exprString(methodField.Type)
									embedPos := pkg.Fset.Position(methodField.Pos())
									relations = append(relations, Relation{
										Kind:           "EMBEDS",
										SourceID:       typeID,
										TargetTypeText: typeStr,
										File:           embedPos.Filename,
										Line:           embedPos.Line,
									})
									continue
								}
								for _, methodName := range methodField.Names {
									methodPos := pkg.Fset.Position(methodName.Pos())
									sigStr := exprString(methodField.Type)
									methodID := typeID + "." + methodName.Name
									entities = append(entities, Entity{
										ID:            methodID,
										Kind:          "METHOD",
										Name:          methodName.Name,
										QualifiedName: methodID,
										Package:       pkg.PkgPath,
										File:          methodPos.Filename,
										Line:          methodPos.Line,
										Column:        methodPos.Column,
										Signature:     sigStr,
										ReceiverType:  typeName, // The interface name
									})
									relations = append(relations, Relation{
										Kind:     "HAS_METHOD",
										SourceID: typeID,
										TargetID: methodID,
										File:     methodPos.Filename,
										Line:     methodPos.Line,
									})
								}
							}

						default:
							// Everything else: `type X int`, `type X func(...)`, `type X map[...]`, etc.
							// All captured as TYPE_ALIAS.
							// alias_kind distinguishes:
							//   - "named_type" for `type X int` (X is a new distinct type)
							//   - "type_alias" for `type X = int` (X and int are interchangeable)
							underlyingStr := exprString(t)
							aliasKind := "named_type"
							if typeSpec.Assign != token.NoPos {
								// typeSpec.Assign is the position of "=" in `type X = T`.
								// NoPos means no "=" — so it's a named type.
								aliasKind = "type_alias"
							}
							entities = append(entities, Entity{
								ID:             typeID,
								Kind:           "TYPE_ALIAS",
								Name:           typeName,
								QualifiedName:  typeID,
								Package:        pkg.PkgPath,
								File:           typePos.Filename,
								Line:           typePos.Line,
								Column:         typePos.Column,
								UnderlyingType: underlyingStr,
								AliasKind:      aliasKind,
							})
						}
					}

				case *ast.FuncDecl:
					// `func F() {...}` (top-level) or `func (r *raft) M() {...}` (method).
					// Distinguished by whether d.Recv (the receiver) is nil.
					funcPos := pkg.Fset.Position(d.Name.Pos())
					sigStr := exprString(d.Type)

					if d.Recv == nil {
						// Top-level function
						funcID := pkg.PkgPath + "." + d.Name.Name
						entities = append(entities, Entity{
							ID:            funcID,
							Kind:          "FUNCTION",
							Name:          d.Name.Name,
							QualifiedName: funcID,
							Package:       pkg.PkgPath,
							File:          funcPos.Filename,
							Line:          funcPos.Line,
							Column:        funcPos.Column,
							Signature:     sigStr,
						})
					} else {
						// Method. Receiver type may be `raft` or `*raft`; we want the type name only.
						recvType := exprString(d.Recv.List[0].Type)
						recvTypeClean := strings.TrimPrefix(recvType, "*")
						recvID := pkg.PkgPath + "." + recvTypeClean
						methodID := recvID + "." + d.Name.Name
						entities = append(entities, Entity{
							ID:            methodID,
							Kind:          "METHOD",
							Name:          d.Name.Name,
							QualifiedName: methodID,
							Package:       pkg.PkgPath,
							File:          funcPos.Filename,
							Line:          funcPos.Line,
							Column:        funcPos.Column,
							Signature:     sigStr,
							ReceiverType:  recvTypeClean,
						})
						// Link method to its receiver via HAS_METHOD.
						// If recvID doesn't match a STRUCT we extracted (e.g. methods on external types
						// or named types like StateType), the relation points to a non-existent entity.
						// Layer 2 can detect dangling references when it loads the graph.
						relations = append(relations, Relation{
							Kind:     "HAS_METHOD",
							SourceID: recvID,
							TargetID: methodID,
							File:     funcPos.Filename,
							Line:     funcPos.Line,
						})
					}
					// === Phase 1b: CALLS edges ===
					callerID := pkg.PkgPath + "." + d.Name.Name
					if d.Recv != nil {
						recvClean := strings.TrimPrefix(exprString(d.Recv.List[0].Type), "*")
						callerID = pkg.PkgPath + "." + recvClean + "." + d.Name.Name
					}
					if d.Body != nil {
						ast.Inspect(d.Body, func(n ast.Node) bool {
							call, ok := n.(*ast.CallExpr)
							if !ok {
								return true
							}
							var id *ast.Ident
							switch fun := call.Fun.(type) {
							case *ast.Ident: // free function: stepCandidate(...)
								id = fun
							case *ast.SelectorExpr: // method/qualified: r.becomeCandidate(...)
								id = fun.Sel
							default:
								return true
							}
							// Calls through function-pointer FIELDS (r.step(...)) resolve to
							// a *types.Var, not a *types.Func, so they're skipped here — that
							// dispatch is an assignment, not a static call.
							fn, ok := pkg.TypesInfo.ObjectOf(id).(*types.Func)
							if !ok {
								return true
							}
							target := calleeID(fn)
							if target == "" || !strings.HasPrefix(target, pkg.PkgPath+".") {
								return true // external (raftpb, fmt, …) or unresolved
							}
							key := callerID + "\x00" + target
							if seenCall[key] {
								return true
							}
							seenCall[key] = true
							callPos := pkg.Fset.Position(call.Lparen)
							relations = append(relations, Relation{
								Kind:     "CALLS",
								SourceID: callerID,
								TargetID: target,
								File:     callPos.Filename,
								Line:     callPos.Line,
							})
							return true
						})
					}
				}
			}
		}

		// === Phase 2: types-based IMPLEMENTS detection ===
		// The Go AST doesn't say "X implements Y" anywhere — that's computed implicitly
		// at use sites by the compiler. We make it explicit via go/types.
		// For each (struct, interface) pair, check whether the pointer-to-struct
		// method set satisfies the interface.
		scope := pkg.Types.Scope()
		var ifaces []*types.Named
		var structs []*types.Named
		for _, name := range scope.Names() {
			obj := scope.Lookup(name)
			// We only care about TypeName objects (vs Func, Var, Const).
			typeName, ok := obj.(*types.TypeName)
			if !ok {
				continue
			}
			// *types.Named is a defined type with a name.
			// Anonymous types (like the underlying type of a struct field) aren't Named.
			named, ok := typeName.Type().(*types.Named)
			if !ok {
				continue
			}
			switch named.Underlying().(type) {
			case *types.Interface:
				ifaces = append(ifaces, named)
			case *types.Struct:
				structs = append(structs, named)
			}
		}
		// Cross-product: every struct against every interface.
		// Cheap: etcd raft has ~22 structs × ~4 interfaces = ~88 checks.
		for _, s := range structs {
			for _, iface := range ifaces {
				ifaceType := iface.Underlying().(*types.Interface)
				// Empty interfaces (TraceLogger here) would match everything; skip.
				if ifaceType.NumMethods() == 0 {
					continue
				}
				// Why NewPointer: most Go methods use pointer receivers. *S's method set
				// is a superset of S's method set, so checking *S catches the common case.
				if types.Implements(types.NewPointer(s), ifaceType) {
					relations = append(relations, Relation{
						Kind:            "IMPLEMENTS",
						SourceID:        pkg.PkgPath + "." + s.Obj().Name(),
						TargetID:        pkg.PkgPath + "." + iface.Obj().Name(),
						SourceIsPointer: true,
					})
				}
			}
		}
	}

	// === Output ===
	graph := Graph{
		SchemaVersion: "0.1",
		Package:       "go.etcd.io/raft/v3",
		Entities:      entities,
		Relations:     relations,
	}

	// Write to disk. File lives in tools/ast_walker/ for now;
	// the Python pipeline will read or symlink it from there.
	f, err := os.Create("raft_graph.json")
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()

	// SetIndent → pretty-printed JSON for human inspection.
	// For larger graphs you might switch to compact and rely on `jq` for browsing.
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	if err := enc.Encode(graph); err != nil {
		log.Fatal(err)
	}

	// Summary to stdout so you can sanity-check without opening the file.
	fmt.Printf("Wrote %d entities and %d relations to raft_graph.json\n",
		len(entities), len(relations))
}
