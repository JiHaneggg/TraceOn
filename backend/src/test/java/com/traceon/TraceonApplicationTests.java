package com.traceon;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest(properties="traceon.manifest=src/test/resources/manifest.json")
class TraceonApplicationTests {

	@Test
	void contextLoads() {
	}

}
