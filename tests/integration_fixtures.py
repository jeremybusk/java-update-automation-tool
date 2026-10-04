"""Actual builds for the documented migration matrix."""
from pathlib import Path

CASES = ('maven17', 'gradle17', 'maven8', 'gradle8', 'maven-bom', 'maven-multi',
         'gradle-catalog', 'gradle-multi', 'boot35-maven', 'boot4-maven', 'boot35-gradle', 'boot4-gradle')


def create(case: str, source: Path) -> tuple[str, int, dict]:
    tool = 'maven' if 'maven' in case else 'gradle'
    target = 25 if case in {'maven8', 'gradle8'} else 21
    initial = 8 if target == 25 else 17
    boot = '3.4.2' if case.startswith('boot35') else '3.5.1' if case.startswith('boot4') else None
    desired_boot = '4.0.x' if case.startswith('boot4') else '3.5.x'
    multi = case.endswith('-multi')
    package_root = source / 'app' if multi else source
    package_root.mkdir(parents=True, exist_ok=True)
    (source / '.gitignore').write_text('target/\nbuild/\n.gradle/\n')
    if tool == 'maven':
        header = '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
        parent = f'<parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>{boot}</version></parent>' if boot else ''
        management = '<dependencyManagement><dependencies><dependency><groupId>org.junit</groupId><artifactId>junit-bom</artifactId><version>5.11.4</version><type>pom</type><scope>import</scope></dependency></dependencies></dependencyManagement>' if case == 'maven-bom' else ''
        dependencies = '<dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency><dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter</artifactId></dependency>' if boot else '<dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>' + ('' if management else '<version>5.11.4</version>') + '<scope>test</scope></dependency>'
        props = f'<properties><maven.compiler.release>{initial}</maven.compiler.release></properties>'
        plugins = '<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.5.2</version></plugin></plugins></build>'
        if multi:
            (source / 'pom.xml').write_text(header + '<groupId>example</groupId><artifactId>parent</artifactId><version>1</version><packaging>pom</packaging><modules><module>app</module></modules>' + props + plugins + '</project>')
            (package_root / 'pom.xml').write_text(header + '<parent><groupId>example</groupId><artifactId>parent</artifactId><version>1</version></parent><artifactId>service</artifactId><dependencies>' + dependencies + '</dependencies></project>')
        else:
            (source / 'pom.xml').write_text(header + parent + '<groupId>example</groupId><artifactId>service</artifactId><version>1</version>' + props + management + '<dependencies>' + dependencies + '</dependencies>' + plugins + '</project>')
    else:
        (source / 'settings.gradle').write_text("rootProject.name = 'service'\n" + ("include 'app'\n" if multi else ''))
        plugins = "plugins { id 'java'" + (f"; id 'org.springframework.boot' version '{boot}'" if boot else '') + ' }\n'
        level = 'VERSION_1_8' if initial == 8 else 'VERSION_17'
        configuration = "repositories { mavenCentral() }\njava { sourceCompatibility = JavaVersion." + level + '; targetCompatibility = JavaVersion.' + level + ' }\n'
        dependency = f"implementation platform('org.springframework.boot:spring-boot-dependencies:{boot}'); implementation 'org.springframework.boot:spring-boot-starter'; testImplementation 'org.springframework.boot:spring-boot-starter-test'" if boot else "testImplementation 'org.junit.jupiter:junit-jupiter:5.11.4'"
        if case == 'gradle-catalog':
            catalog = source / 'gradle/libs.versions.toml'
            catalog.parent.mkdir()
            catalog.write_text('[versions]\njunit = "5.11.4"\n[libraries]\njunit = { module = "org.junit.jupiter:junit-jupiter", version.ref = "junit" }\n')
            dependency = 'testImplementation libs.junit'
        configuration += 'dependencies { ' + dependency + "; testRuntimeOnly 'org.junit.platform:junit-platform-launcher' }\ntest { useJUnitPlatform() }\n"
        if multi:
            (source / 'build.gradle').write_text("subprojects { apply plugin: 'java'\n" + configuration + '}\n')
            (package_root / 'build.gradle').write_text('// Build configured by parent.\n')
        else:
            (source / 'build.gradle').write_text(plugins + configuration)
    main = package_root / 'src/main/java/example'
    main.mkdir(parents=True)
    (main / 'Service.java').write_text('package example; public class Service { public String value() { return "ok"; } }\n')
    tests = package_root / 'src/test/java/example'
    tests.mkdir(parents=True)
    (tests / 'ServiceTest.java').write_text('package example; import org.junit.jupiter.api.Test; import static org.junit.jupiter.api.Assertions.assertEquals; public class ServiceTest { @Test public void contract() { assertEquals("ok", new Service().value()); } }\n')
    if boot:
        (main / 'Service.java').write_text('package example; import org.springframework.boot.SpringBootVersion; public class Service { public String value() { return SpringBootVersion.getVersion() != null ? "ok" : "missing"; } }\n')
    config = {'schema_version': 1, 'targets': {'java': {'desired': target, 'acceptable': [target]},
              'spring_boot': {'desired': desired_boot, 'acceptable': [desired_boot]}},
              'migration': {'profile': 'conservative', 'verification': {'build': 'test', 'postChecks': 'none', 'strict': True},
                            'openrewrite': {'recipe_repository': 'codegenome' if boot else 'maven-central',
                                            'artifacts': ['org.openrewrite.recipe:rewrite-spring:6.40.0'] if boot else []}},
              'workflow': {'mode': 'unattended'}}
    return tool, target, config
