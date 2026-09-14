class Continuum < Formula
  include Language::Python::Virtualenv

  desc "Verifiable semantic recovery layer for long-running AI agents"
  homepage "https://github.com/Cyrax321/CONTINUUM"
  url "https://github.com/Cyrax321/CONTINUUM/archive/refs/tags/v0.1.2.tar.gz"
  sha256 "1bfdc57b87fe7570fac38fd3c91bc28df0de51d7a74c1ed1e3cb2e3a4598f8ba"
  license "Apache-2.0"

  depends_on "python@3.12"

  def install
    virtualenv_create(libexec, "python3")
    system libexec/"bin"/"pip", "install", "-v", "--no-deps",
           "--no-build-isolation", "--ignore-installed", buildpath
    system libexec/"bin"/"pip", "install", "-v",
           "--ignore-installed", "pydantic>=2.7"
    bin.install_symlink Dir[libexec/"bin"/"continuum*"]
  end

  test do
    assert_match "continuum", shell_output("#{bin}/continuum --help")
    system bin/"continuum", "--version"
  end
end
