use super::*;
use std::path::Path;
use std::process::Command as StdCommand;

struct GitFixture {
    directory: tempfile::TempDir,
}

impl GitFixture {
    fn new() -> Self {
        let directory = tempfile::tempdir().expect("temporary repository");
        git(directory.path(), &["init", "--quiet"]);
        git(directory.path(), &["config", "user.name", "Test Agent"]);
        git(
            directory.path(),
            &["config", "user.email", "test@example.invalid"],
        );
        Self { directory }
    }

    fn path(&self) -> &Path {
        self.directory.path()
    }

    fn commit_file(&self, name: &str, contents: &str, message: &str) -> String {
        std::fs::write(self.path().join(name), contents).expect("write test file");
        git(self.path(), &["add", name]);
        git(self.path(), &["commit", "--quiet", "-m", message]);
        git(self.path(), &["rev-parse", "HEAD"])
    }

    fn head(&self) -> String {
        git(self.path(), &["rev-parse", "HEAD"])
    }

    fn tree(&self, commit: &str) -> String {
        git(self.path(), &["rev-parse", &format!("{commit}^{{tree}}")])
    }
}

#[tokio::test]
async fn accepts_ancestor_history_and_exact_tree() {
    let fixture = GitFixture::new();
    let base = fixture.commit_file("base.txt", "base\n", "base");
    let child = fixture.commit_file("child.txt", "reviewed child\n", "child");
    let head = fixture.head();
    let tree = fixture.tree(&head);
    let commits = vec![child, base];
    let digest = ancestor_proof_digest(&head, &tree, &commits);

    verify_parent_aggregate_lineage(fixture.path(), &head, &tree, &commits, &digest)
        .await
        .expect("reviewed child commits are ancestors of the aggregate");
}

#[tokio::test]
async fn accepts_uniquely_patch_equivalent_rebased_child_history() {
    let fixture = GitFixture::new();
    let base = fixture.commit_file("base.txt", "base\n", "base");
    let original_child = fixture.commit_file("child.txt", "reviewed child\n", "child");
    let original_patch = git_bytes(
        fixture.path(),
        &["diff", "--binary", "--no-ext-diff", &base, &original_child],
    );
    git(fixture.path(), &["checkout", "--quiet", &base]);
    apply_patch(fixture.path(), &original_patch);
    let rebased = fixture.commit_file("child.txt", "reviewed child\n", "rebased child");
    let head = fixture.head();
    let tree = fixture.tree(&head);
    let commits = vec![original_child.clone(), base.clone()];
    let digest = proof_digest(
        &head,
        &tree,
        vec![
            CommitRelation {
                aggregate_commit: rebased,
                receipt_commit: original_child,
                relation: "patch_equivalent_rebase",
            },
            CommitRelation {
                aggregate_commit: base.clone(),
                receipt_commit: base,
                relation: "ancestor",
            },
        ],
    );

    verify_parent_aggregate_lineage(fixture.path(), &head, &tree, &commits, &digest)
        .await
        .expect("a uniquely rebased reviewed commit is corroborated by Git");
}

#[tokio::test]
async fn rejects_unrelated_aggregate_even_when_head_and_tree_are_valid() {
    let fixture = GitFixture::new();
    let base = fixture.commit_file("base.txt", "base\n", "base");
    let child = fixture.commit_file("child.txt", "reviewed child\n", "child");
    git(fixture.path(), &["checkout", "--quiet", &base]);
    fixture.commit_file("unrelated.txt", "other\n", "unrelated");
    let head = fixture.head();
    let tree = fixture.tree(&head);
    let digest = ancestor_proof_digest(&head, &tree, std::slice::from_ref(&child));

    let error = verify_parent_aggregate_lineage(
        fixture.path(),
        &head,
        &tree,
        std::slice::from_ref(&child),
        &digest,
    )
    .await
    .expect_err("unrelated work must not inherit the child receipt");
    assert!(error
        .to_string()
        .contains("does not contain the completed child patch"));
}

#[tokio::test]
async fn rejects_ambiguous_patch_equivalent_history() {
    let fixture = GitFixture::new();
    let base = fixture.commit_file("base.txt", "base\n", "base");
    let child = fixture.commit_file("same.txt", "same patch\n", "child");
    let child_patch = git_bytes(
        fixture.path(),
        &["diff", "--binary", "--no-ext-diff", &base, &child],
    );
    git(fixture.path(), &["checkout", "--quiet", &base]);
    apply_patch(fixture.path(), &child_patch);
    let first_match = fixture.commit_file("same.txt", "same patch\n", "first equivalent");
    git(fixture.path(), &["rm", "same.txt"]);
    git(fixture.path(), &["commit", "--quiet", "-m", "remove file"]);
    apply_patch(fixture.path(), &child_patch);
    fixture.commit_file("same.txt", "same patch\n", "second equivalent");
    let head = fixture.head();
    let tree = fixture.tree(&head);
    let commits = vec![child.clone()];
    let digest = proof_digest(
        &head,
        &tree,
        vec![CommitRelation {
            aggregate_commit: first_match,
            receipt_commit: child,
            relation: "patch_equivalent_rebase",
        }],
    );

    let error = verify_parent_aggregate_lineage(fixture.path(), &head, &tree, &commits, &digest)
        .await
        .expect_err("multiple equivalent patches are ambiguous");
    assert!(error.to_string().contains("ambiguous patch-equivalent"));
}

#[tokio::test]
async fn rejects_changed_tree_and_stale_lineage_digest() {
    let fixture = GitFixture::new();
    let base = fixture.commit_file("base.txt", "base\n", "base");
    let child = fixture.commit_file("child.txt", "reviewed child\n", "child");
    let head = fixture.head();
    let tree = fixture.tree(&head);
    let digest = ancestor_proof_digest(&head, &tree, std::slice::from_ref(&child));
    let wrong_tree = "0".repeat(40);
    assert!(verify_parent_aggregate_lineage(
        fixture.path(),
        &head,
        &wrong_tree,
        std::slice::from_ref(&child),
        &digest,
    )
    .await
    .unwrap_err()
    .to_string()
    .contains("differs from the approved watcher tree"));

    let commits = vec![base, child];
    assert!(verify_parent_aggregate_lineage(
        fixture.path(),
        &head,
        &tree,
        &commits,
        &"a".repeat(64),
    )
    .await
    .unwrap_err()
    .to_string()
    .contains("proof differs"));
}

fn ancestor_proof_digest(head: &str, tree: &str, commits: &[String]) -> String {
    let sorted = commits.iter().collect::<BTreeSet<_>>();
    let relations = sorted
        .into_iter()
        .map(|commit| CommitRelation {
            aggregate_commit: commit.clone(),
            receipt_commit: commit.clone(),
            relation: "ancestor",
        })
        .collect();
    proof_digest(head, tree, relations)
}

fn proof_digest(head: &str, tree: &str, mut relations: Vec<CommitRelation>) -> String {
    relations.sort_by(|left, right| left.receipt_commit.cmp(&right.receipt_commit));
    let proof = json!({
        "aggregate_head_sha": head,
        "aggregate_tree_sha": tree,
        "relations": relations,
    });
    format!("{:x}", Sha256::digest(serde_json::to_vec(&proof).unwrap()))
}

fn apply_patch(repository: &Path, patch: &[u8]) {
    let mut command = StdCommand::new("git");
    command
        .arg("-C")
        .arg(repository)
        .args(["apply", "--binary"])
        .stdin(Stdio::piped());
    let mut child = command.spawn().expect("start git apply");
    use std::io::Write;
    child.stdin.take().unwrap().write_all(patch).unwrap();
    assert!(child.wait().unwrap().success(), "apply test patch");
}

fn git(repository: &Path, arguments: &[&str]) -> String {
    let output = git_command(repository, arguments)
        .output()
        .expect("run git");
    assert!(
        output.status.success(),
        "git {arguments:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)
        .expect("git output is utf-8")
        .trim()
        .to_string()
}

fn git_bytes(repository: &Path, arguments: &[&str]) -> Vec<u8> {
    let output = git_command(repository, arguments)
        .output()
        .expect("run git");
    assert!(
        output.status.success(),
        "git {arguments:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

fn git_command<'a>(repository: &Path, arguments: &'a [&'a str]) -> StdCommand {
    let mut command = StdCommand::new("git");
    command.arg("-C").arg(repository).args(arguments);
    command
}
