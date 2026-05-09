# toolbox

Holding pen for assets and bundles.

## holyland

Source tarball + design docs of the **Holy Land** game project. Lives here because the sandbox that produced it is allowlisted only to `wyrdlands`, `hexmaster`, and `toolbox` — direct push to a separate `holy-land` repo isn't possible from inside the sandbox.

- `holyland-PLAN.md` — full design plan: rendering style decision, language pick, three deployment targets, two-tier input architecture, portability rules, Session 1 scope.
- `holyland-ROADMAP.md` — session-by-session forward plan. What's done (S1H1 + S3 ECS/saves), what's deferred (S1H2 Miyoo, S2 Android), what's ahead (oasis, NPCs, harvest, wilderness, demons, blessings, ruin biomes).
- `holyland.tar.gz` — full source (Rust + SDL2 + gilrs + image + hecs + serde + ciborium + uuid), Guybrush CP437 16x16 tileset, plan + roadmap copies inside. ~21KB. No `target/`, no `Cargo.lock`.

To extract and use:

```bash
mkdir holy-land && cd holy-land
tar -xzf path/to/holyland.tar.gz
cargo run --release      # first build is slow; bundles SDL2 from source
cargo test --release     # save/load round-trip + future-schema-rejection tests
```

Then `git init` / `git remote add` / `git push` from your own machine to `campjun/holy-land`.

